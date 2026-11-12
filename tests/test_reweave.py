import copy
import json
import sqlite3
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path

from warehouse.catalog import connect, load_bundle, load_file
from warehouse.definition import UNKNOWN_ENTITY, UNKNOWN_SURROGATE
from warehouse.reweave import (GenerationConflict, preview, publish,
                               query_dimensions, query_facts, query_mart,
                               summarize)
from warehouse.resolution import build_timeline, canonical_key, ingest_resolution, locate

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'examples/source.json'
RESOLUTIONS = {
    'merge': ROOT / 'examples/resolution-1-merge.json',
    'split': ROOT / 'examples/resolution-2-split.json',
    'backfill': ROOT / 'examples/resolution-3-backfill.json',
    'reattach': ROOT / 'examples/resolution-4-reattach.json',
}


def load_source(connection, batch='daily'):
    load_file(connection, SOURCE, batch)


def load_named(connection, *names):
    for name in names:
        ingest_resolution(connection, json.loads(RESOLUTIONS[name].read_text(encoding='utf-8')))


def total_amount(mart):
    return sum((Decimal(row['amount_sum']) for row in mart), Decimal('0'))


class ResolutionValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.connection = connect(Path(self.temporary.name) / 'w.sqlite3')
        self.document = json.loads(RESOLUTIONS['merge'].read_text(encoding='utf-8'))

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def test_replay_same_content_is_noop(self):
        self.assertTrue(ingest_resolution(self.connection, self.document))
        self.assertFalse(ingest_resolution(self.connection, copy.deepcopy(self.document)))
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM resolutions').fetchone()[0], 1)

    def test_same_id_version_different_content_blocks(self):
        ingest_resolution(self.connection, self.document)
        changed = copy.deepcopy(self.document)
        changed['entities'].append('ent-zeta')
        with self.assertRaises(ValueError):
            ingest_resolution(self.connection, changed)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM resolutions').fetchone()[0], 1)

    def test_duplicate_mapping_same_version(self):
        changed = copy.deepcopy(self.document)
        changed['mappings'].append(copy.deepcopy(changed['mappings'][0]))
        with self.assertRaises(ValueError):
            ingest_resolution(self.connection, changed)

    def test_identity_interval_conflict_same_version(self):
        changed = copy.deepcopy(self.document)
        changed['mappings'][1]['valid_from'] = '2024-02-01'
        changed['mappings'][1]['valid_to'] = '2024-03-01'
        # 与 crm:a 不冲突；构造同一身份的第二条重叠片段
        changed['mappings'].append({
            'system': 'crm', 'source_key': 'a', 'entity': 'ent-alpha',
            'valid_from': '2024-02-01', 'valid_to': '2024-03-01',
        })
        with self.assertRaises(ValueError):
            ingest_resolution(self.connection, changed)

    def test_touching_intervals_are_allowed(self):
        changed = copy.deepcopy(self.document)
        changed['valid_to'] = '2024-12-31'
        changed['mappings'][2]['valid_to'] = '2024-02-01'
        changed['mappings'].append({
            'system': 'crm', 'source_key': 'd', 'entity': 'ent-beta',
            'valid_from': '2024-02-01', 'valid_to': '2024-12-31',
        })
        ingest_resolution(self.connection, changed)

    def test_illegal_entity_reference(self):
        changed = copy.deepcopy(self.document)
        changed['mappings'][0]['entity'] = 'ent-not-declared'
        with self.assertRaises(ValueError):
            ingest_resolution(self.connection, changed)

    def test_reserved_unknown_entity(self):
        changed = copy.deepcopy(self.document)
        changed['entities'][0] = UNKNOWN_ENTITY
        changed['mappings'][0]['entity'] = UNKNOWN_ENTITY
        with self.assertRaises(ValueError):
            ingest_resolution(self.connection, changed)

    def test_cross_document_interval_conflict(self):
        ingest_resolution(self.connection, self.document)
        other = json.loads(RESOLUTIONS['backfill'].read_text(encoding='utf-8'))
        other['mappings'][0]['system'] = 'crm'
        other['mappings'][0]['source_key'] = 'a'
        with self.assertRaises(ValueError):
            ingest_resolution(self.connection, other)

    def test_version_must_increase(self):
        v2 = copy.deepcopy(self.document)
        v2['version'] = 2
        v2['valid_from'] = '2024-05-01'
        v2['valid_to'] = '2024-12-31'
        for mapping in v2['mappings']:
            mapping['valid_from'] = '2024-05-01'
            mapping['valid_to'] = '2024-12-31'
        ingest_resolution(self.connection, v2)
        stale = copy.deepcopy(v2)
        stale['version'] = 1
        with self.assertRaises(ValueError):
            ingest_resolution(self.connection, stale)

    def test_bad_interval_and_date(self):
        changed = copy.deepcopy(self.document)
        changed['valid_to'] = '2023-12-31'
        with self.assertRaises(ValueError):
            ingest_resolution(self.connection, changed)
        changed = copy.deepcopy(self.document)
        changed['mappings'][0]['valid_from'] = 'not-a-date'
        with self.assertRaises(ValueError):
            ingest_resolution(self.connection, changed)

    def test_canonical_key(self):
        self.assertEqual(canonical_key('crm', 'a'), 'crm:a')
        with self.assertRaises(ValueError):
            canonical_key('crm:x', 'a')
        with self.assertRaises(ValueError):
            canonical_key('', 'a')

    def test_locate_uses_occurrence_date_not_whole_history(self):
        ingest_resolution(self.connection, self.document)
        timeline = build_timeline(self.connection)
        from datetime import date
        entity, segment = locate(timeline, 'crm:d', date(2024, 3, 31))
        self.assertEqual(entity, 'ent-beta')
        entity, _ = locate(timeline, 'crm:d', date(2024, 5, 1))
        self.assertEqual(entity, UNKNOWN_ENTITY)

    def test_mapping_outside_document_window(self):
        changed = copy.deepcopy(self.document)
        changed['mappings'][0]['valid_from'] = '2023-12-31'
        with self.assertRaises(ValueError):
            ingest_resolution(self.connection, changed)

    def test_missing_required_field(self):
        changed = copy.deepcopy(self.document)
        del changed['mappings'][0]['entity']
        with self.assertRaises(ValueError):
            ingest_resolution(self.connection, changed)

    def test_bad_system_identifier(self):
        changed = copy.deepcopy(self.document)
        changed['mappings'][0]['system'] = 'bad:system'
        with self.assertRaises(ValueError):
            ingest_resolution(self.connection, changed)

    def test_open_ended_interval_blocks_later_segment(self):
        changed = copy.deepcopy(self.document)
        changed['valid_to'] = None
        for mapping in changed['mappings']:
            mapping['valid_to'] = None
        ingest_resolution(self.connection, changed)
        later = copy.deepcopy(changed)
        later['version'] = 2
        later['valid_from'] = '2024-06-01'
        later['valid_to'] = '2024-12-31'
        for mapping in later['mappings']:
            mapping['valid_from'] = '2024-06-01'
            mapping['valid_to'] = '2024-12-31'
        with self.assertRaises(ValueError):
            ingest_resolution(self.connection, later)

    def test_empty_mappings_rejected(self):
        changed = copy.deepcopy(self.document)
        changed['mappings'] = []
        with self.assertRaises(ValueError):
            ingest_resolution(self.connection, changed)


class ReweaveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.connection = connect(Path(self.temporary.name) / 'w.sqlite3')
        load_source(self.connection)

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def _publish_all(self):
        generations = []
        for name in ('merge', 'split', 'backfill', 'reattach'):
            load_named(self.connection, name)
            generations.append(publish(self.connection))
        return generations

    def test_g1_unknown_dimension_then_backfill(self):
        load_named(self.connection, 'merge')
        report = publish(self.connection)
        self.assertEqual(report['generation'], 1)
        _, facts = query_facts(self.connection, 1)
        # 未被任何决议覆盖的身份/日期先进入未知维，来源键原样保留
        # merge 决议只覆盖到 2024-04-01，故 4 月 1 日的 c5 与 lms/erp 身份同样未知
        unknown = {row['contract'] for row in facts if row['surrogate'] == UNKNOWN_SURROGATE}
        self.assertEqual(unknown, {'c5', 'c6', 'c7', 'c8', 'c9'})
        self.assertTrue(all(row['source_key'] for row in facts))
        dimensions = query_dimensions(self.connection)
        self.assertEqual(dimensions[0], {'surrogate': UNKNOWN_SURROGATE, 'entity': UNKNOWN_ENTITY})
        # 补决议后，需要回填的事实在新一代次定位，旧代次保持未知
        load_named(self.connection, 'backfill')
        report3 = publish(self.connection)
        _, facts3 = query_facts(self.connection, report3['generation'])
        dimensions3 = {row['entity']: row['surrogate'] for row in query_dimensions(self.connection)}
        delta_surrogate = dimensions3['ent-delta']
        self.assertTrue(all(row['surrogate'] == delta_surrogate
                            for row in facts3 if row['contract'] in {'c6', 'c7'}))
        _, old_facts = query_facts(self.connection, 1)
        self.assertTrue(all(row['surrogate'] == UNKNOWN_SURROGATE
                            for row in old_facts if row['contract'] in {'c6', 'c7'}))

    def test_split_creates_fresh_surrogate_and_keeps_history(self):
        load_named(self.connection, 'merge')
        publish(self.connection)
        load_named(self.connection, 'split')
        report = publish(self.connection)
        self.assertEqual(report['scanned_months'], ['2024-04'])
        _, facts = query_facts(self.connection, report['generation'])
        by_contract = {row['contract']: row['surrogate'] for row in facts}
        # 3 月历史仍归旧实体，4 月新事实归拆分实体；新实体取得全新代理键
        self.assertNotEqual(by_contract['c4'], by_contract['c5'])
        dimensions = {row['entity']: row['surrogate'] for row in query_dimensions(self.connection)}
        self.assertEqual(by_contract['c4'], dimensions['ent-beta'])
        self.assertEqual(by_contract['c5'], dimensions['ent-gamma'])
        # 代理键不得复用其他实体的历史身份
        self.assertEqual(len(set(dimensions.values())), len(dimensions))
        self.assertGreater(dimensions['ent-gamma'], dimensions['ent-beta'])

    def test_reattach_does_not_move_whole_history(self):
        reports = self._publish_all()
        generation = reports[-1]['generation']
        _, facts = query_facts(self.connection, generation)
        by_contract = {row['contract']: row for row in facts}
        dimensions = {row['entity']: row['surrogate'] for row in query_dimensions(self.connection)}
        # 决议 3 月 1 日生效：3 月起的事实归新实体，更早的历史留在未知维
        self.assertEqual(by_contract['c8']['surrogate'], dimensions['ent-epsilon'])
        self.assertEqual(by_contract['c9']['surrogate'], dimensions['ent-epsilon'])

    def test_interval_boundary_is_half_open(self):
        # 4 月 1 日当天的事实属于新片段（valid_from <= day < valid_to）
        load_named(self.connection, 'merge', 'split')
        report = publish(self.connection)
        _, facts = query_facts(self.connection, report['generation'])
        dimensions = {row['entity']: row['surrogate'] for row in query_dimensions(self.connection)}
        c5 = next(row for row in facts if row['contract'] == 'c5')
        self.assertEqual(c5['surrogate'], dimensions['ent-gamma'])
        self.assertEqual(c5['signed_on'], '2024-04-01')

    def test_amount_conserved_every_generation(self):
        reports = self._publish_all()
        expected = Decimal('58.00')
        for report in reports:
            _, mart = query_mart(self.connection, report['generation'])
            self.assertEqual(total_amount(mart), expected)

    def test_active_customers_use_distinct_surrogates_not_amount_logic(self):
        reports = self._publish_all()
        _, g3 = query_mart(self.connection, reports[2]['generation'])
        # G3 的 2024-03：ent-beta(crm:d)、ent-delta(lms:f)、未知(erp:h) 三个活跃客户
        march = [row for row in g3 if row['month'] == '2024-03']
        self.assertEqual(len(march), 3)
        self.assertEqual(summarize(g3)['active_customers_by_month']['2024-03'], 3)
        # 同一客户同月两份合同：活跃客户只算一个，但金额两份都计入
        dimensions = {row['entity']: row['surrogate'] for row in query_dimensions(self.connection)}
        delta_mart = next(row for row in g3
                          if row['month'] == '2024-02' and row['surrogate'] == dimensions['ent-delta'])
        self.assertEqual(delta_mart['active_contracts'], 1)
        self.assertEqual(delta_mart['amount_sum'], '4.00')
        # 金额守恒并不保证人数正确：拆分后 4 月客户数从 1 变为 2，金额不变
        _, g1_mart = query_mart(self.connection, reports[0]['generation'])
        april_g1 = [row for row in g1_mart if row['month'] == '2024-04']
        april_g3 = [row for row in g3 if row['month'] == '2024-04']
        self.assertEqual(sum((Decimal(r['amount_sum']) for r in april_g1), Decimal('0')),
                         sum((Decimal(r['amount_sum']) for r in april_g3), Decimal('0')))
        self.assertEqual(len(april_g1), 1)
        self.assertEqual(len(april_g3), 2)

    def test_partition_pruning_unaffected_months_not_read(self):
        load_named(self.connection, 'merge')
        publish(self.connection)
        read_months = []

        def reader(connection, month):
            read_months.append(month)
            return [dict(row) for row in connection.execute(
                'SELECT contract, source_key, amount, signed_on FROM contracts '
                'WHERE signed_on LIKE ? ORDER BY contract', (month + '-%',))]

        load_named(self.connection, 'split')  # 只触及 2024-04
        report = publish(self.connection, month_reader=reader)
        self.assertEqual(read_months, ['2024-04'])
        self.assertEqual(report['scanned_months'], ['2024-04'])
        # 未受影响月份从代次 1 复制，查询结果仍然完整
        _, mart = query_mart(self.connection, report['generation'])
        self.assertEqual(total_amount(mart), Decimal('58.00'))
        self.assertEqual({row['month'] for row in mart},
                         {'2024-01', '2024-02', '2024-03', '2024-04'})

    def test_preview_does_not_persist_shadow_results(self):
        load_named(self.connection, 'merge')
        report = preview(self.connection)
        self.assertIsNone(report['generation'])
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM generations').fetchone()[0], 0)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM dim_entity').fetchone()[0], 0)
        # 预览给出受影响合同与分区，供维护者先看再提交
        contracts = {row['contract'] for row in report['rewritten_facts']}
        self.assertEqual(contracts, {f'c{i}' for i in range(1, 10)})
        published = publish(self.connection)
        self.assertEqual(published['generation'], 1)

    def test_republish_unchanged_is_idempotent(self):
        load_named(self.connection, 'merge')
        first = publish(self.connection)
        again = publish(self.connection)
        self.assertTrue(again['unchanged'])
        self.assertEqual(again['generation'], first['generation'])
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM generations').fetchone()[0], 1)

    def test_old_generation_remains_queryable(self):
        reports = self._publish_all()
        g1, g4 = reports[0]['generation'], reports[-1]['generation']
        gen1, mart1 = query_mart(self.connection, g1)
        gen4, mart4 = query_mart(self.connection, g4)
        self.assertEqual((gen1, gen4), (1, 4))
        self.assertNotEqual(mart1, mart4)
        self.assertEqual(total_amount(mart1), total_amount(mart4))
        with self.assertRaises(ValueError):
            query_mart(self.connection, 999)

    def test_late_contract_lands_in_new_generation_never_shadow(self):
        load_named(self.connection, 'merge')
        g1 = publish(self.connection)
        # 重织期间到达的合同先进入来源事实，不被任何影子结果覆盖
        late_bundle = {
            'contracts': [{
                'contract': 'c10', 'source_key': 'crm:a',
                'amount': '3.00', 'signed_on': '2024-04-15',
            }],
        }
        load_bundle(self.connection, late_bundle, 'late')
        # 已发布的旧代次看不到迟到合同
        _, old_facts = query_facts(self.connection, g1['generation'])
        self.assertNotIn('c10', {row['contract'] for row in old_facts})
        shadow = preview(self.connection)
        self.assertIsNone(shadow['generation'])
        # 预览同样不改变已发布数据
        _, still_old = query_facts(self.connection)
        self.assertNotIn('c10', {row['contract'] for row in still_old})
        g_new = publish(self.connection)
        _, facts = query_facts(self.connection, g_new['generation'])
        c10 = next(row for row in facts if row['contract'] == 'c10')
        self.assertEqual(c10['month'], '2024-04')
        self.assertEqual(c10['amount'], '3.00')
        # 迟到分区按当前决议新算并落到已知实体而非未知维
        dimensions = {row['entity']: row['surrogate']
                      for row in query_dimensions(self.connection)}
        self.assertEqual(c10['surrogate'], dimensions['ent-alpha'])
        _, mart = query_mart(self.connection, g_new['generation'])
        self.assertEqual(total_amount(mart), Decimal('61.00'))
        # 只有迟到合同所在月份被扫描，旧分区全部复用
        self.assertEqual(g_new['scanned_months'], ['2024-04'])

    def test_two_publishers_never_expose_intermediate_state(self):
        load_named(self.connection, 'merge')
        db_path = Path(self.temporary.name) / 'publishers.sqlite3'
        bundle = json.loads(SOURCE.read_text(encoding='utf-8'))
        merge_doc = json.loads(RESOLUTIONS['merge'].read_text(encoding='utf-8'))
        errors = []
        barrier = threading.Barrier(2)

        def worker():
            connection = connect(db_path)
            try:
                load_bundle(connection, bundle, 'daily')
                ingest_resolution(connection, merge_doc)
                barrier.wait()
                publish(connection)
            except (sqlite3.OperationalError, GenerationConflict, RuntimeError) as exc:
                errors.append(exc)
            finally:
                connection.close()

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start(); t2.start(); t1.join(); t2.join()
        verifier = connect(db_path)
        # 恰好一个发布者成功；读者始终只能看到完整代次，不存在半成品
        count = verifier.execute('SELECT COUNT(*) FROM generations').fetchone()[0]
        self.assertEqual(count, 1)
        pointer = verifier.execute('SELECT generation FROM published_pointer').fetchone()
        self.assertEqual(pointer['generation'], 1)
        fact_count = verifier.execute(
            'SELECT COUNT(*) FROM fact_rw WHERE generation=1').fetchone()[0]
        mart_count = verifier.execute(
            'SELECT COUNT(*) FROM mart_rw WHERE generation=1').fetchone()[0]
        self.assertEqual(fact_count, 9)
        self.assertGreater(mart_count, 0)
        # 锁随提交释放，后续发布可正常进入
        self.assertEqual(verifier.execute('SELECT COUNT(*) FROM publication_locks').fetchone()[0], 0)
        verifier.close()
        self.assertLessEqual(len(errors), 1)

    def test_failed_publish_leaves_no_partial_state(self):
        load_named(self.connection, 'merge')

        class FailingConnection:
            def __init__(self, inner):
                self._inner = inner
                self.row_factory = inner.row_factory

            def execute(self, sql, parameters=()):
                if sql.startswith('INSERT INTO generations'):
                    raise RuntimeError('模拟代次写入中断')
                return self._inner.execute(sql, parameters)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        with self.assertRaises(RuntimeError):
            publish(FailingConnection(self.connection))
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM generations').fetchone()[0], 0)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM fact_rw').fetchone()[0], 0)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM mart_rw').fetchone()[0], 0)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM dim_entity').fetchone()[0], 0)
        # 失败后可用相同输入与映射重新发布成功（恢复只依赖未变化的输入与指纹）
        retry = publish(self.connection)
        self.assertEqual(retry['generation'], 1)
        self.assertEqual(total_amount(query_mart(self.connection, 1)[1]), Decimal('58.00'))

    def test_fingerprints_drive_partition_reuse(self):
        load_named(self.connection, 'merge')
        g1 = publish(self.connection)
        load_named(self.connection, 'split')
        g2 = publish(self.connection)
        # 2024-04 输入指纹未变但映射指纹变化 -> 重算；其他月份双指纹不变 -> 复用
        builds = {row['month']: dict(row) for row in self.connection.execute(
            'SELECT * FROM partition_builds WHERE generation=?', (g2['generation'],))}
        prior = {row['month']: dict(row) for row in self.connection.execute(
            'SELECT * FROM partition_builds WHERE generation=?', (g1['generation'],))}
        for month in ('2024-01', '2024-02', '2024-03'):
            self.assertEqual(builds[month]['input_fingerprint'], prior[month]['input_fingerprint'])
            self.assertEqual(builds[month]['mapping_fingerprint'], prior[month]['mapping_fingerprint'])
        self.assertNotEqual(builds['2024-04']['mapping_fingerprint'],
                            prior['2024-04']['mapping_fingerprint'])

    def test_source_keys_are_never_overwritten(self):
        reports = self._publish_all()
        _, facts = query_facts(self.connection, reports[-1]['generation'])
        source_rows = {row['contract']: row['source_key']
                       for row in self.connection.execute('SELECT contract, source_key FROM contracts')}
        for row in facts:
            self.assertEqual(row['source_key'], source_rows[row['contract']])

    def test_publish_without_resolutions_puts_everything_in_unknown(self):
        report = publish(self.connection)
        self.assertEqual(report['scanned_months'], ['2024-01', '2024-02', '2024-03', '2024-04'])
        _, mart = query_mart(self.connection, report['generation'])
        self.assertTrue(all(row['surrogate'] == UNKNOWN_SURROGATE for row in mart))
        self.assertEqual(total_amount(mart), Decimal('58.00'))

    def test_unknown_only_generation_then_backfill_moves_facts(self):
        g0 = publish(self.connection)
        _, before = query_facts(self.connection, g0['generation'])
        self.assertTrue(all(row['surrogate'] == UNKNOWN_SURROGATE for row in before))
        load_named(self.connection, 'backfill')
        g1 = publish(self.connection)
        self.assertEqual(g1['scanned_months'], ['2024-02', '2024-03'])
        _, after = query_facts(self.connection, g1['generation'])
        delta = next(d['surrogate'] for d in query_dimensions(self.connection)
                     if d['entity'] == 'ent-delta')
        self.assertTrue(all(row['surrogate'] == delta
                            for row in after if row['source_key'] == 'lms:f'))
        # 其他身份仍在未知维
        self.assertTrue(all(row['surrogate'] == UNKNOWN_SURROGATE
                            for row in after if row['source_key'] != 'lms:f'))

    def test_invalid_amount_fact_is_rejected_at_build(self):
        load_bundle(self.connection, {
            'contracts': [{
                'contract': 'cx', 'source_key': 'crm:a',
                'amount': 'NaN', 'signed_on': '2024-01-05',
            }],
        }, 'nan-batch')
        load_named(self.connection, 'merge')
        with self.assertRaises(ValueError):
            publish(self.connection)

    def test_preview_then_interleaved_publishes_keep_surrogate_consistency(self):
        # 回归：预览投影只覆盖受影响月份，持久化必须用全量事实的权威代理键重建
        load_named(self.connection, 'merge', 'split')
        projected = preview(self.connection)['surrogates']
        g1 = publish(self.connection)
        load_named(self.connection, 'backfill')
        g2 = publish(self.connection)
        load_named(self.connection, 'reattach')
        g3 = publish(self.connection)
        _, mart_last = query_mart(self.connection, g3['generation'])
        self.assertEqual(total_amount(mart_last), Decimal('58.00'))
        self.assertEqual(len(mart_last), 9)
        # 全部代次事实完整且金额守恒
        for generation in (g1['generation'], g2['generation'], g3['generation']):
            _, facts = query_facts(self.connection, generation)
            self.assertEqual(len(facts), 9)
            _, mart = query_mart(self.connection, generation)
            self.assertEqual(total_amount(mart), Decimal('58.00'))
        # 预览中出现过的实体键与最终维度一致，未发生重排
        dimensions = {row['entity']: row['surrogate']
                      for row in query_dimensions(self.connection)}
        for entity, surrogate in projected.items():
            self.assertEqual(dimensions[entity], surrogate)


if __name__ == '__main__':
    unittest.main()
