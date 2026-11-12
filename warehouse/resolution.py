"""上游拆分/合并决议的本地装载与时间线定位。

决议是上游给定的只读输入：带唯一编号、递增版本、生效区间，以及来源身份
（系统名 + 主键）到统一实体的完整映射。本模块不做候选匹配、人工确认或
客户维护，只负责校验、幂等入库和按发生时间定位归属。
"""
import json
from datetime import date
from pathlib import Path

from .catalog import encode, transaction
from .definition import UNKNOWN_ENTITY

_FAR_FUTURE = date.max


def canonical_key(system, source_key):
    """来源身份的规范化形式：系统名与主键组合成合同 source_key 可比对的键。"""
    if not isinstance(system, str) or not system or ':' in system:
        raise ValueError('来源系统名必须为不含冒号的非空字符串')
    if not isinstance(source_key, str) or not source_key or ':' in source_key:
        raise ValueError('来源主键必须为不含冒号的非空字符串')
    return f'{system}:{source_key}'


def _parse_day(value, field):
    if not isinstance(value, str):
        raise ValueError(f'{field} 必须是 ISO 日期字符串')
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{field} 日期格式非法: {value!r}') from exc


def _parse_interval(mapping, prefix=''):
    start = _parse_day(mapping['valid_from'], f'{prefix}valid_from')
    end_text = mapping.get('valid_to')
    end = _parse_day(end_text, f'{prefix}valid_to') if end_text is not None else None
    if end is not None and not start < end:
        raise ValueError(f'{prefix}生效区间必须满足 valid_from < valid_to')
    return start, end


def _validate_document(document):
    if not isinstance(document, dict):
        raise ValueError('决议必须是 JSON 对象')
    required = ['resolution_id', 'version', 'valid_from', 'valid_to', 'entities', 'mappings']
    missing = [field for field in required if field not in document]
    if missing:
        raise ValueError(f'决议缺少必填字段: {", ".join(missing)}')
    resolution_id = document['resolution_id']
    if not isinstance(resolution_id, str) or not resolution_id:
        raise ValueError('决议编号必须为非空字符串')
    version = document['version']
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError('决议版本必须是不小于 1 的整数')
    doc_start, doc_end = _parse_interval(document)
    entities = document['entities']
    if not isinstance(entities, list) or not entities:
        raise ValueError('决议必须声明非空实体清单')
    if any(not isinstance(entity, str) or not entity for entity in entities):
        raise ValueError('实体标识必须为非空字符串')
    if UNKNOWN_ENTITY in entities:
        raise ValueError(f'实体标识 {UNKNOWN_ENTITY} 为系统保留值')
    if len(set(entities)) != len(entities):
        raise ValueError('同版决议存在重复实体声明')
    legal = set(entities)
    mappings = document['mappings']
    if not isinstance(mappings, list) or not mappings:
        raise ValueError('决议必须包含至少一条来源身份映射')
    normalized = []
    seen_identities = set()
    intervals_by_identity = {}
    for index, mapping in enumerate(mappings):
        if not isinstance(mapping, dict):
            raise ValueError(f'第 {index} 条映射不是对象')
        for field in ['system', 'source_key', 'entity', 'valid_from', 'valid_to']:
            if field not in mapping:
                raise ValueError(f'第 {index} 条映射缺少字段 {field}')
        if mapping['entity'] not in legal:
            raise ValueError(f'第 {index} 条映射引用了未在实体清单中声明的实体')
        identity = canonical_key(mapping['system'], mapping['source_key'])
        start, end = _parse_interval(mapping, prefix=f'映射 {identity} ')
        within_end = (doc_end is None and end is None) or (doc_end is not None and end is not None and end <= doc_end)
        if not (doc_start <= start and within_end):
            raise ValueError(f'映射 {identity} 的生效区间超出决议生效区间')
        # 同版重复：同一身份、同一生效起点
        identity_start = (identity, start.isoformat())
        if identity_start in seen_identities:
            raise ValueError(f'同版决议存在重复映射: {identity} 自 {start}')
        seen_identities.add(identity_start)
        # 同一身份区间冲突（半开区间，端点相接允许）
        for other_start, other_end in intervals_by_identity.get(identity, []):
            if start < (other_end or _FAR_FUTURE) and other_start < (end or _FAR_FUTURE):
                raise ValueError(f'同一来源身份 {identity} 的生效区间冲突')
        intervals_by_identity.setdefault(identity, []).append((start, end))
        normalized.append({
            'system': mapping['system'],
            'source_key': mapping['source_key'],
            'identity': identity,
            'entity': mapping['entity'],
            'start': start,
            'end': end,
        })
    return {
        'resolution_id': resolution_id,
        'version': version,
        'start': doc_start,
        'end': doc_end,
        'entities': entities,
        'mappings': normalized,
    }


def ingest_resolution(connection, document):
    """装载一条决议。同编号同版本同内容重放无影响，内容不同则阻断。"""
    normalized = _validate_document(document)
    digest = encode(document)
    resolution_id = normalized['resolution_id']
    version = normalized['version']
    with transaction(connection):
        existing = connection.execute(
            'SELECT digest FROM resolutions WHERE resolution_id=? AND version=?',
            (resolution_id, version),
        ).fetchone()
        if existing is not None:
            if existing['digest'] != digest:
                raise ValueError(f'决议 {resolution_id} 版本 {version} 内容不一致，禁止改写已收决议')
            return False
        previous_versions = connection.execute(
            'SELECT MAX(version) AS v FROM resolutions WHERE resolution_id=?',
            (resolution_id,),
        ).fetchone()['v']
        if previous_versions is not None and version <= previous_versions:
            raise ValueError(f'决议 {resolution_id} 版本必须递增，已有版本 {previous_versions}')
        # 跨决议、跨版本的同一身份区间冲突检查
        for mapping in normalized['mappings']:
            rows = connection.execute(
                'SELECT valid_from, valid_to FROM resolution_mappings '
                'WHERE system=? AND source_key=?',
                (mapping['system'], mapping['source_key']),
            ).fetchall()
            for row in rows:
                other_start = date.fromisoformat(row['valid_from'])
                other_end = date.fromisoformat(row['valid_to']) if row['valid_to'] else None
                if mapping['start'] < (other_end or _FAR_FUTURE) and other_start < (mapping['end'] or _FAR_FUTURE):
                    raise ValueError(
                        f"来源身份 {mapping['identity']} 与已收决议区间冲突: "
                        f"{row['valid_from']} ~ {row['valid_to'] or '开放'}"
                    )
        connection.execute(
            'INSERT INTO resolutions VALUES (?, ?, ?, ?, ?)',
            (resolution_id, version, digest, normalized['start'].isoformat(),
             normalized['end'].isoformat() if normalized['end'] else None),
        )
        for entity in normalized['entities']:
            connection.execute(
                'INSERT INTO resolution_entities VALUES (?, ?, ?)',
                (resolution_id, version, entity),
            )
        for mapping in normalized['mappings']:
            connection.execute(
                'INSERT INTO resolution_mappings VALUES (?, ?, ?, ?, ?, ?, ?)',
                (resolution_id, version, mapping['system'], mapping['source_key'],
                 mapping['entity'], mapping['start'].isoformat(),
                 mapping['end'].isoformat() if mapping['end'] else None),
            )
    return True


def load_resolution_file(connection, path):
    return ingest_resolution(connection, json.loads(Path(path).read_text(encoding='utf-8')))


def build_timeline(connection):
    """汇总所有已收决议映射，按来源身份给出排序后的生效片段。"""
    timeline = {}
    rows = connection.execute(
        'SELECT system, source_key, entity, valid_from, valid_to, resolution_id, version '
        'FROM resolution_mappings ORDER BY valid_from'
    )
    for row in rows:
        identity = canonical_key(row['system'], row['source_key'])
        timeline.setdefault(identity, []).append({
            'entity': row['entity'],
            'start': date.fromisoformat(row['valid_from']),
            'end': date.fromisoformat(row['valid_to']) if row['valid_to'] else None,
            'resolution_id': row['resolution_id'],
            'version': row['version'],
        })
    for segments in timeline.values():
        segments.sort(key=lambda segment: segment['start'])
    return timeline


def locate(timeline, identity, day):
    """按事实发生时间定位实体；没有覆盖该日期的映射时进入未知维。

    某来源身份跨期改属时，只取覆盖该事实日期的片段，绝不把整段历史
    无条件挪到新客户。
    """
    for segment in timeline.get(identity, ()):
        if segment['start'] <= day and (segment['end'] is None or day < segment['end']):
            return segment['entity'], segment
    return UNKNOWN_ENTITY, None
