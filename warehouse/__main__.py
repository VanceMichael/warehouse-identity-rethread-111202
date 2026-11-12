import argparse
import json
import sqlite3
from pathlib import Path

from .catalog import connect, load_file
from .domain import overview
from .reweave import (generations, preview, publish, query_dimensions,
                      query_facts, query_mart, summarize)
from .resolution import load_resolution_file
from .store import ObjectStore


def _write_output(payload, output):
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if output:
        Path(output).write_text(text + '\n', encoding='utf-8')
    return payload


def main():
    parser = argparse.ArgumentParser(description='本地数据集合装载、决议接收与事实重织')
    parser.add_argument('command', choices=[
        'import', 'inspect', 'export',
        'resolution', 'preview', 'publish', 'generations',
        'mart', 'facts', 'dimensions', 'summary',
    ])
    parser.add_argument('--db', required=True)
    parser.add_argument('--input')
    parser.add_argument('--batch')
    parser.add_argument('--objects')
    parser.add_argument('--generation', type=int)
    parser.add_argument('--out')
    args = parser.parse_args()
    if args.command == 'import' and (not args.input or not args.batch):
        parser.error('导入需要 --input 和 --batch')
    if args.command == 'export' and not args.objects:
        parser.error('导出需要 --objects')
    if args.command in ('resolution',) and not args.input:
        parser.error('决议接收需要 --input')
    if args.command == 'preview' and not args.out:
        parser.error('差异预览需要 --out 指定预览文件，维护者确认后再发布')
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    connection = connect(args.db)
    try:
        if args.command == 'import':
            result = {'changed': load_file(connection, args.input, args.batch)}
        elif args.command == 'inspect':
            result = overview(connection)
        elif args.command == 'export':
            records = overview(connection)
            result = {'object': ObjectStore(args.objects).put(records), 'rows': len(records)}
        elif args.command == 'resolution':
            result = {'changed': load_resolution_file(connection, args.input)}
        elif args.command == 'preview':
            report = preview(connection)
            result = _write_output(report, args.out)
        elif args.command == 'publish':
            result = publish(connection)
        elif args.command == 'generations':
            result = generations(connection)
        elif args.command == 'mart':
            generation, rows = query_mart(connection, args.generation)
            result = {'generation': generation, 'rows': rows}
        elif args.command == 'facts':
            generation, rows = query_facts(connection, args.generation)
            result = {'generation': generation, 'rows': rows}
        elif args.command == 'dimensions':
            result = query_dimensions(connection)
        elif args.command == 'summary':
            generation, rows = query_mart(connection, args.generation)
            result = {'generation': generation, **summarize(rows)}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    except (ValueError, RuntimeError, sqlite3.Error) as exc:
        parser.exit(status=1, message=f'错误: {exc}\n')
    finally:
        connection.close()


if __name__ == '__main__':
    main()
