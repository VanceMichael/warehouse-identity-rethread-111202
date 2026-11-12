"""面向既有事实的归属重织。

按选择的决议版本为既有合同事实生成按代次（generation）的代理键与集市
结果，不覆盖来源键、不修改原始事实。一次发布把维度版本、事实代理键和
集市快照作为同一可读代次原子切换；旧报告仍可按原代次查询。
"""
import hashlib
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal

from .catalog import encode, transaction
from .definition import UNKNOWN_ENTITY, UNKNOWN_SURROGATE
from .resolution import build_timeline, canonical_key, locate


class GenerationConflict(RuntimeError):
    """两个发布者并发产生代次冲突，调用方应重试；任何中间态都不对外可见。"""


def _sha(parts):
    return hashlib.sha256(encode(parts)).hexdigest()


def decision_fingerprint(connection):
    rows = [tuple(row) for row in connection.execute(
        'SELECT resolution_id, version FROM resolutions ORDER BY resolution_id, version')]
    return _sha(rows)


def _mapping_fingerprint(timeline):
    payload = [
        (identity, [(segment['entity'], segment['start'].isoformat(),
                     None if segment['end'] is None else segment['end'].isoformat())
                    for segment in sorted(segments, key=lambda item: item['start'])])
        for identity, segments in sorted(timeline.items())
    ]
    return _sha(payload)


def _base_fact_fingerprint(connection):
    rows = [tuple(row) for row in connection.execute(
        'SELECT contract, source_key, amount, signed_on FROM contracts ORDER BY contract')]
    return _sha(rows)


def _month_sql(month):
    # contracts.signed_on 为 ISO 日期，月份分区按前缀裁剪，避免扫描无关月份
    return month + '-%'


def _all_months(connection):
    return [row[0] for row in connection.execute(
        "SELECT DISTINCT substr(signed_on, 1, 7) AS month FROM contracts ORDER BY month")]


def _read_partition(connection, month, month_reader):
    if month_reader is not None:
        return month_reader(connection, month)
    return [dict(row) for row in connection.execute(
        'SELECT contract, source_key, amount, signed_on FROM contracts '
        'WHERE signed_on LIKE ? ORDER BY contract', (_month_sql(month),))]


def _existing_surrogates(connection):
    return {row['entity']: row['surrogate']
            for row in connection.execute('SELECT entity, surrogate FROM dim_entity')}


def project_surrogates(connection, timeline, facts):
    """预览态的实体->代理键投影：已存在键保持不变，新实体只分配不落库。

    拆分产生的实体从未出现过，因此取得全新代理键；任何实体都不会复用
    其他实体已经占用的历史身份键。
    """
    surrogates = _existing_surrogates(connection)
    surrogates[UNKNOWN_ENTITY] = UNKNOWN_SURROGATE
    next_key = max([UNKNOWN_SURROGATE, *surrogates.values()]) + 1
    for fact in facts:
        day = date.fromisoformat(fact['signed_on'])
        entity, _ = locate(timeline, fact['source_key'], day)
        if entity not in surrogates:
            surrogates[entity] = next_key
            next_key += 1
    return surrogates


def _allocate_dimensions(connection, timeline, facts):
    surrogates = project_surrogates(connection, timeline, facts)
    for entity, surrogate in sorted(surrogates.items(), key=lambda item: item[1]):
        if entity == UNKNOWN_ENTITY:
            continue
        connection.execute('INSERT OR IGNORE INTO dim_entity (surrogate, entity) VALUES (?, ?)',
                           (surrogate, entity))
    return {row['entity']: row['surrogate']
            for row in connection.execute('SELECT entity, surrogate FROM dim_entity')} | {
        UNKNOWN_ENTITY: UNKNOWN_SURROGATE}


def _next_month_start(month_start):
    return date(month_start.year + (month_start.month // 12),
                (month_start.month % 12) + 1, 1)


def _partition_input_fingerprint(connection, month):
    rows = [tuple(row) for row in connection.execute(
        'SELECT contract, source_key, amount, signed_on FROM contracts '
        'WHERE signed_on LIKE ? ORDER BY contract', (month + '-%',))]
    return _sha(rows)


def _affected_months(connection, previous_generation):
    """只重算映射区间与事实分区交叠、且确有受影响身份事实的月份。

    另外两类分区也必须重算：历史代次中不存在的月份（迟到合同带来的新分区），
    以及输入指纹已变化的既有月份（同月到达的新合同）。
    """
    months = set(_all_months(connection))
    if previous_generation is None:
        return sorted(months)
    prior_builds = {row['month']: row for row in connection.execute(
        'SELECT month, input_fingerprint, mapping_fingerprint FROM partition_builds '
        'WHERE generation=?', (previous_generation,))}
    forced = {month for month in months
              if month not in prior_builds
              or prior_builds[month]['input_fingerprint']
              != _partition_input_fingerprint(connection, month)}
    previous_decisions = connection.execute(
        'SELECT resolution_id, MAX(version) AS version FROM generation_decisions '
        'WHERE generation=? GROUP BY resolution_id', (previous_generation,)).fetchall()
    previous_map = {row['resolution_id']: row['version'] for row in previous_decisions}
    current_map = {row['resolution_id']: row['version']
                   for row in connection.execute(
                       'SELECT resolution_id, MAX(version) AS version FROM resolutions '
                       'GROUP BY resolution_id')}
    changed_ids = set(current_map) - set(previous_map)
    for resolution_id, version in current_map.items():
        if resolution_id in previous_map and previous_map[resolution_id] != version:
            changed_ids.add(resolution_id)
    if not changed_ids:
        # 决议未变但可能有迟到/新增事实，按输入指纹变化决定重算分区
        return sorted(forced)
    placeholders = ','.join('?' for _ in changed_ids)
    # 变化决议触及的来源身份，以及这些身份的实际生效片段
    segments = []
    identities = set()
    for row in connection.execute(
            f'SELECT system, source_key, valid_from, valid_to '
            f'FROM resolution_mappings WHERE resolution_id IN ({placeholders})',
            tuple(sorted(changed_ids))):
        identity = canonical_key(row['system'], row['source_key'])
        identities.add(identity)
        segments.append((
            identity,
            date.fromisoformat(row['valid_from']),
            date.fromisoformat(row['valid_to']) if row['valid_to'] else None,
        ))
    if not identities:
        return sorted(forced)
    key_placeholders = ','.join('?' for _ in identities)
    # 这些身份的事实实际落在哪些月份；无事实的月份不扫描
    fact_months = connection.execute(
        f'SELECT DISTINCT source_key, substr(signed_on, 1, 7) AS month '
        f'FROM contracts WHERE source_key IN ({key_placeholders})',
        tuple(sorted(identities))).fetchall()
    touched = set(forced)
    for row in fact_months:
        month_start = date.fromisoformat(row['month'] + '-01')
        month_end = _next_month_start(month_start)
        for identity, start, end in segments:
            if identity != row['source_key']:
                continue
            if start < month_end and (end is None or end > month_start):
                touched.add(row['month'])
                break
    return sorted(touched & months)


def _build_partition(month, facts, timeline, surrogates, mapping_fingerprint):
    rewritten = []
    aggregates = {}
    for fact in facts:
        day = date.fromisoformat(fact['signed_on'])
        entity, _segment = locate(timeline, fact['source_key'], day)
        amount = Decimal(fact['amount'])
        if not amount.is_finite():
            raise ValueError('数量必须为有限十进制')
        surrogate = surrogates[entity]
        rewritten.append({
            'contract': fact['contract'],
            'source_key': fact['source_key'],
            'signed_on': fact['signed_on'],
            'month': month,
            'surrogate': surrogate,
            'amount': str(amount),
        })
        bucket = aggregates.setdefault(surrogate, {'amount': Decimal('0'), 'contracts': set()})
        # 累计签约额：每份合同按全额贡献一次
        bucket['amount'] += amount
        # 去重活跃客户：同一客户在该月无论多少份合同只算一个
        bucket['contracts'].add(fact['contract'])
    mart = [
        {'month': month, 'surrogate': surrogate,
         'amount_sum': str(values['amount']), 'active_contracts': len(values['contracts'])}
        for surrogate, values in sorted(aggregates.items())
    ]
    input_fingerprint = _sha([
        (fact['contract'], fact['source_key'], fact['amount'], fact['signed_on'])
        for fact in sorted(facts, key=lambda item: item['contract'])
    ])
    return rewritten, mart, input_fingerprint, mapping_fingerprint


def _copy_partition_from_previous(connection, previous_generation, month, generation):
    connection.execute(
        'INSERT INTO fact_rw (generation, contract, source_key, signed_on, month, surrogate, amount) '
        'SELECT ?, contract, source_key, signed_on, month, surrogate, amount FROM fact_rw '
        'WHERE generation=? AND month=?', (generation, previous_generation, month))
    connection.execute(
        'INSERT INTO mart_rw (generation, month, surrogate, amount_sum, active_contracts) '
        'SELECT ?, month, surrogate, amount_sum, active_contracts FROM mart_rw '
        'WHERE generation=? AND month=?', (generation, previous_generation, month))
    connection.execute(
        'INSERT INTO partition_builds (generation, month, input_fingerprint, mapping_fingerprint) '
        'SELECT ?, month, input_fingerprint, mapping_fingerprint FROM partition_builds '
        'WHERE generation=? AND month=?', (generation, previous_generation, month))


def _compute_report(connection, timeline, *, month_reader, previous, unchanged=False):
    mapping_fingerprint = _mapping_fingerprint(timeline)
    base_fingerprint = _base_fact_fingerprint(connection)
    months = sorted(set(_all_months(connection)))
    affected = [] if unchanged else _affected_months(connection, previous)
    # 每个分区只读取一次，且只有受影响分区才会被读取；无关月份完全不扫描
    partitions = {month: _read_partition(connection, month, month_reader)
                  for month in affected}
    all_facts = [fact for facts in partitions.values() for fact in facts]
    # 预览只做不落库的代理键投影；持久化时在 _work 中按全量事实正式分配
    surrogates = project_surrogates(connection, timeline, all_facts)

    partition_reports = []
    built = {}
    raw = {}
    for month in affected:
        facts = partitions[month]
        raw[month] = facts
        rewritten, mart, input_fp, mapping_fp = _build_partition(
            month, facts, timeline, surrogates, mapping_fingerprint)
        built[month] = (rewritten, mart, input_fp, mapping_fp)
        reused_from = None
        if previous is not None:
            prior = connection.execute(
                'SELECT input_fingerprint, mapping_fingerprint FROM partition_builds '
                'WHERE generation=? AND month=?', (previous, month)).fetchone()
            if prior is not None and prior['input_fingerprint'] == input_fp \
                    and prior['mapping_fingerprint'] == mapping_fp:
                reused_from = previous
        partition_reports.append({
            'month': month,
            'contracts': len(facts),
            'reused_from_generation': reused_from,
            'input_fingerprint': input_fp,
            'mapping_fingerprint': mapping_fp,
        })

    current_decisions = [tuple(row) for row in connection.execute(
        'SELECT resolution_id, MAX(version) AS version FROM resolutions '
        'GROUP BY resolution_id ORDER BY resolution_id')]
    return {
        'generation': None,
        'previous_generation': previous,
        'unchanged': unchanged,
        'decision_fingerprint': decision_fingerprint(connection),
        'base_fact_fingerprint': base_fingerprint,
        'mapping_fingerprint': mapping_fingerprint,
        'all_months': months,
        'affected_months': affected,
        'scanned_months': affected,
        'partition_reports': partition_reports,
        'rewritten_facts': [row for month in affected for row in built[month][0]],
        'mart': [row for month in affected for row in built[month][1]],
        'decisions': [{'resolution_id': rid, 'version': version}
                      for rid, version in current_decisions],
        'surrogates': dict(sorted(surrogates.items(), key=lambda item: item[1])),
        '_built': built,
        '_raw': raw,
    }


def build_generation(connection, *, month_reader=None, persist=False, publish=False):
    """按当前全部已收决议构建新一代次；persist=False 时只做差异预览。

    publish=True 时在单个事务内完成代次行、维度、事实、集市与发布指针的
    全部写入，读者要么看到完整旧代次，要么看到完整新代次。两个发布者并发
    调用时只有一个能拿到发布锁，输者收到 GenerationConflict，不暴露中间态。
    """
    if not persist and publish:
        raise ValueError('发布必须持久化')

    def _work():
        if publish:
            holder = f'publisher-{datetime.now(timezone.utc).isoformat()}-{id(connection)}'
            try:
                connection.execute(
                    'INSERT INTO publication_locks (slot, holder, acquired_at) VALUES (1, ?, ?)',
                    (holder, datetime.now(timezone.utc).isoformat()))
            except sqlite3.IntegrityError as exc:
                raise GenerationConflict('另一发布者正在发布，请重试') from exc
        previous = connection.execute('SELECT MAX(generation) AS g FROM generations').fetchone()['g']
        timeline = build_timeline(connection)
        base_fingerprint = _base_fact_fingerprint(connection)
        decision_fp = decision_fingerprint(connection)
        unchanged = False
        if previous is not None:
            prior = connection.execute(
                'SELECT decision_fingerprint, base_fact_fingerprint FROM generations '
                'WHERE generation=?', (previous,)).fetchone()
            # 决议与输入都未变化时重发无影响：复用最新代次，不制造空代次。
            # 输入指纹变化（含同月迟到合同）会在 _affected_months 中强制重算，
            # 因此这里只需比较代次级的决议与输入摘要。
            unchanged = prior['decision_fingerprint'] == decision_fp \
                and prior['base_fact_fingerprint'] == base_fingerprint
        report = _compute_report(connection, timeline,
                                 month_reader=month_reader, previous=previous,
                                 unchanged=unchanged)
        if not persist:
            report.pop('_built', None)
            report.pop('_raw', None)
            return report
        if report['unchanged']:
            report['generation'] = previous
            if publish:
                connection.execute('DELETE FROM publication_locks WHERE slot=1')
                report['published'] = True
            return report

        # 先按全量事实正式分配代理键：受影响分区与未受影响月份中的新实体
        # 必须共用同一套权威键，避免预览投影与持久化键编号错位
        all_facts = [dict(row) for row in connection.execute(
            'SELECT contract, source_key, amount, signed_on FROM contracts ORDER BY contract')]
        surrogates = _allocate_dimensions(connection, timeline, all_facts)
        # 用权威代理键在内存中重建受影响分区，不再第二次读取分区数据
        rebuilt = {}
        rewritten_total = []
        mart_total = []
        for month in report['affected_months']:
            rewritten, mart, input_fp, mapping_fp = _build_partition(
                month, report['_raw'][month], timeline, surrogates, report['mapping_fingerprint'])
            rebuilt[month] = (rewritten, mart, input_fp, mapping_fp)
            rewritten_total.extend(rewritten)
            mart_total.extend(mart)
        report['_built'] = rebuilt
        report['rewritten_facts'] = rewritten_total
        report['mart'] = mart_total
        report['surrogates'] = dict(sorted(surrogates.items(), key=lambda item: item[1]))
        cursor = connection.execute(
            'INSERT INTO generations (created_at, decision_fingerprint, base_fact_fingerprint) '
            'VALUES (?, ?, ?)',
            (datetime.now(timezone.utc).isoformat(),
             report['decision_fingerprint'], base_fingerprint))
        generation = cursor.lastrowid
        for item in report['decisions']:
            connection.execute('INSERT INTO generation_decisions VALUES (?, ?, ?)',
                               (generation, item['resolution_id'], item['version']))
        for month in report['all_months']:
            if month in report['_built']:
                rewritten, mart, input_fp, mapping_fp = report['_built'][month]
                for row in rewritten:
                    connection.execute(
                        'INSERT INTO fact_rw VALUES (?, ?, ?, ?, ?, ?, ?)',
                        (generation, row['contract'], row['source_key'], row['signed_on'],
                         month, row['surrogate'], row['amount']))
                for row in mart:
                    connection.execute(
                        'INSERT INTO mart_rw VALUES (?, ?, ?, ?, ?)',
                        (generation, month, row['surrogate'], row['amount_sum'],
                         row['active_contracts']))
                connection.execute('INSERT INTO partition_builds VALUES (?, ?, ?, ?)',
                                   (generation, month, input_fp, mapping_fp))
            else:
                # 未受影响分区：输入与映射指纹均未变化，直接从上一代次复制
                prior_build = connection.execute(
                    'SELECT input_fingerprint, mapping_fingerprint FROM partition_builds '
                    'WHERE generation=? AND month=?', (previous, month)).fetchone()
                if prior_build is None:
                    # 理论不可达：新月份已在受影响集合中；保留防御避免静默丢分区
                    raise RuntimeError(f'缺少可复用的分区构建记录: {month}')
                _copy_partition_from_previous(connection, previous, month, generation)
        report['generation'] = generation
        report.pop('_built', None)
        report.pop('_raw', None)
        if publish:
            connection.execute(
                'INSERT INTO published_pointer (slot, generation) VALUES (1, ?) '
                'ON CONFLICT(slot) DO UPDATE SET generation=excluded.generation',
                (generation,))
            # 成功提交前释放互斥锁；失败时随事务回滚自动释放
            connection.execute('DELETE FROM publication_locks WHERE slot=1')
            report['published'] = True
        return report

    if persist:
        with transaction(connection):
            return _work()
    return _work()


def preview(connection, month_reader=None):
    return build_generation(connection, month_reader=month_reader, persist=False)


def publish(connection, month_reader=None):
    return build_generation(connection, month_reader=month_reader, persist=True, publish=True)


def published_generation(connection):
    row = connection.execute('SELECT generation FROM published_pointer WHERE slot=1').fetchone()
    return None if row is None else row['generation']


def generations(connection):
    rows = connection.execute(
        'SELECT g.generation, g.created_at, g.decision_fingerprint, g.base_fact_fingerprint, '
        "EXISTS (SELECT 1 FROM published_pointer p WHERE p.generation=g.generation) AS published "
        'FROM generations g ORDER BY g.generation').fetchall()
    result = []
    for row in rows:
        decisions = [dict(item) for item in connection.execute(
            'SELECT resolution_id, version FROM generation_decisions '
            'WHERE generation=? ORDER BY resolution_id', (row['generation'],))]
        item = dict(row)
        item['published'] = bool(item['published'])
        item['decisions'] = decisions
        result.append(item)
    return result


def query_mart(connection, generation=None):
    """按代次查询集市；缺省读取当前发布代次，旧报告可显式指定旧代次。"""
    if generation is None:
        generation = published_generation(connection)
        if generation is None:
            raise ValueError('尚无已发布代次')
    exists = connection.execute('SELECT 1 FROM generations WHERE generation=?',
                                (generation,)).fetchone()
    if exists is None:
        raise ValueError(f'代次 {generation} 不存在')
    return generation, [dict(row) for row in connection.execute(
        'SELECT month, surrogate, amount_sum, active_contracts FROM mart_rw '
        'WHERE generation=? ORDER BY month, surrogate', (generation,))]


def query_facts(connection, generation=None):
    if generation is None:
        generation = published_generation(connection)
        if generation is None:
            raise ValueError('尚无已发布代次')
    return generation, [dict(row) for row in connection.execute(
        'SELECT contract, source_key, signed_on, month, surrogate, amount FROM fact_rw '
        'WHERE generation=? ORDER BY contract', (generation,))]


def query_dimensions(connection):
    rows = [dict(row) for row in connection.execute(
        'SELECT surrogate, entity FROM dim_entity ORDER BY surrogate')]
    rows.insert(0, {'surrogate': UNKNOWN_SURROGATE, 'entity': UNKNOWN_ENTITY})
    return rows


def summarize(mart_rows):
    """金额守恒与去重活跃客户的汇总视图，两类指标使用各自的贡献规则。"""
    amount = Decimal('0')
    active_by_month = {}
    for row in mart_rows:
        amount += Decimal(row['amount_sum'])
        active_by_month.setdefault(row['month'], set()).add(row['surrogate'])
    return {
        'total_amount': str(amount),
        'active_customers_by_month': {month: len(surrogates)
                                      for month, surrogates in sorted(active_by_month.items())},
    }


def canonical_identity(system, source_key):
    return canonical_key(system, source_key)
