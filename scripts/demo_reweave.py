"""重织能力端到端演示：全部通过命令行入口驱动，不回放预制结果。

覆盖：连续合并后局部拆分、半开区间边界、未知成员补档、跨期改属不挪整段
历史、迟到合同的代次归属、差异预览、发布与旧版查询，并逐版校验金额守恒
与去重活跃客户数的可解释性。
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def execute(*arguments):
    command = [sys.executable, '-m', 'warehouse', *arguments]
    print('$ ' + ' '.join(command), flush=True)
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=True)
    print(result.stdout.strip(), flush=True)
    return json.loads(result.stdout)


def check(condition, message):
    if not condition:
        raise RuntimeError(message)
    print(f'  ✓ {message}', flush=True)


def main():
    with tempfile.TemporaryDirectory() as temporary:
        database = str(Path(temporary) / 'warehouse.sqlite3')
        examples = ROOT / 'examples'

        print('== 1. 导入合成来源（合同保留原始来源身份与发生时间） ==', flush=True)
        execute('import', '--db', database, '--input', str(examples / 'source.json'), '--batch', 'daily')

        print('== 2. 接收合并决议，先预览受影响合同/分区/指标再发布代次 1 ==', flush=True)
        execute('resolution', '--db', database,
                '--input', str(examples / 'resolution-1-merge.json'))
        preview1 = execute('preview', '--db', database,
                           '--out', str(Path(temporary) / 'preview1.json'))
        check(len(preview1['rewritten_facts']) == 9, '预览列出全部 9 份既有合同')
        check(preview1['scanned_months'] == ['2024-01', '2024-02', '2024-03', '2024-04'],
              '首代扫描全部事实月份')
        g1 = execute('publish', '--db', database)
        check(g1['generation'] == 1, '代次 1 已发布')
        summary1 = execute('summary', '--db', database, '--generation', '1')
        check(summary1['total_amount'] == '58.00', '代次 1 金额守恒 58.00')
        check(summary1['active_customers_by_month']['2024-03'] == 2,
              '3 月只有合并实体与未知维两个活跃客户（金额不代表人数）')
        facts1 = execute('facts', '--db', database, '--generation', '1')
        unknown = {row['contract'] for row in facts1['rows'] if row['surrogate'] == 0}
        check(unknown == {'c5', 'c6', 'c7', 'c8', 'c9'}, '未知成员进入未知维（代理键 0）')
        check(all(row['source_key'] for row in facts1['rows']), '事实保留原始来源键，未被覆盖')

        print('== 3. 重放同内容决议无影响；同编号异内容阻断（由单元测试覆盖 CLI 语义） ==', flush=True)
        replay = execute('resolution', '--db', database,
                         '--input', str(examples / 'resolution-1-merge.json'))
        check(replay['changed'] is False, '同版同内容重放无写入')

        print('== 4. 局部拆分：只重算 2024-04 分区，拆分实体取得全新代理键 ==', flush=True)
        execute('resolution', '--db', database,
                '--input', str(examples / 'resolution-2-split.json'))
        preview2 = execute('preview', '--db', database,
                           '--out', str(Path(temporary) / 'preview2.json'))
        check(preview2['scanned_months'] == ['2024-04'], '拆分仅扫描交叠月份 2024-04')
        g2 = execute('publish', '--db', database)
        check(g2['generation'] == 2, '代次 2 已发布')
        summary2 = execute('summary', '--db', database)
        check(summary2['total_amount'] == '58.00', '代次 2 金额仍守恒 58.00')
        check(summary2['active_customers_by_month']['2024-04'] == 2,
              '4 月拆分后活跃客户变为 2，而金额不变——人数规则独立于金额守恒')
        dimensions = execute('dimensions', '--db', database)
        keys = {row['entity']: row['surrogate'] for row in dimensions}
        check(keys['ent-gamma'] > keys['ent-beta'], '拆分实体不复用旧实体的历史代理键')

        print('== 5. 未知成员补档：回填事实在新一代次定位，旧代次保持未知 ==', flush=True)
        execute('resolution', '--db', database,
                '--input', str(examples / 'resolution-3-backfill.json'))
        g3 = execute('publish', '--db', database)
        dimensions = execute('dimensions', '--db', database)
        facts3 = execute('facts', '--db', database, '--generation', str(g3['generation']))
        delta = next(row['surrogate'] for row in dimensions
                     if row['entity'] == 'ent-delta')
        check(all(row['surrogate'] == delta
                  for row in facts3['rows'] if row['source_key'] == 'lms:f'),
              'lms:f 的历史事实回填到 ent-delta')
        old = execute('facts', '--db', database, '--generation', '1')
        check(all(row['surrogate'] == 0 for row in old['rows'] if row['source_key'] == 'lms:f'),
              '旧代次 1 的报告仍把 lms:f 留在未知维')

        print('== 6. 跨期改属：只移动生效日后的事实，整段历史不无条件搬迁 ==', flush=True)
        execute('resolution', '--db', database,
                '--input', str(examples / 'resolution-4-reattach.json'))
        g4 = execute('publish', '--db', database)
        check(g4['scanned_months'] == ['2024-03', '2024-04'], '改属只扫描 3、4 月分区')
        facts4 = execute('facts', '--db', database, '--generation', str(g4['generation']))
        dimensions = execute('dimensions', '--db', database)
        by_contract = {row['contract']: row for row in facts4['rows']}
        epsilon = next(row['surrogate'] for row in dimensions
                       if row['entity'] == 'ent-epsilon')
        check(by_contract['c8']['surrogate'] == epsilon and by_contract['c8']['signed_on'] >= '2024-03-01',
              '生效日后的 erp:h 事实归 ent-epsilon')
        check(by_contract['c8']['source_key'] == 'erp:h', '来源键始终不变')
        summary4 = execute('summary', '--db', database, '--generation', str(g4['generation']))
        check(summary4['total_amount'] == '58.00', '代次 4 金额仍守恒 58.00')

        print('== 7. 旧版查询入口：每一代次都可独立读取 ==', flush=True)
        gens = execute('generations', '--db', database)
        check([item['generation'] for item in gens] == [1, 2, 3, 4], '四个可读代次全部在册')
        for generation in range(1, 5):
            summary = execute('summary', '--db', database, '--generation', str(generation))
            check(summary['total_amount'] == '58.00', f'代次 {generation} 金额守恒')

        print('== 8. 迟到合同：重织期间到达的事实落入新一代次，不被影子结果覆盖 ==', flush=True)
        late_path = Path(temporary) / 'late.json'
        late_path.write_text(json.dumps({
            'contracts': [{
                'contract': 'c10', 'source_key': 'crm:a',
                'amount': '3.00', 'signed_on': '2024-05-02',
            }],
        }), encoding='utf-8')
        execute('import', '--db', database, '--input', str(late_path), '--batch', 'late')
        execute('preview', '--db', database, '--out', str(Path(temporary) / 'preview5.json'))
        current = execute('facts', '--db', database)
        check('c10' not in {row['contract'] for row in current['rows']},
              '预览是影子结果，未覆盖当前已发布代次')
        g5 = execute('publish', '--db', database)
        facts5 = execute('facts', '--db', database, '--generation', str(g5['generation']))
        c10 = next(row for row in facts5['rows'] if row['contract'] == 'c10')
        check(c10['month'] == '2024-05' and c10['amount'] == '3.00',
              '迟到合同明确落在新一代次的 2024-05 分区')
        summary5 = execute('summary', '--db', database)
        check(summary5['total_amount'] == '61.00', '新代次金额 58.00 + 3.00 = 61.00 守恒')
        check(summary5['active_customers_by_month']['2024-05'] == 1, '5 月去重活跃客户为 1')
        print('全部演示检查通过。', flush=True)


if __name__ == '__main__':
    main()
