from collections import defaultdict
from decimal import Decimal


def overview(connection):
    rows = [dict(record) for record in connection.execute('SELECT i.entity, c.amount FROM contracts c JOIN identities i ON c.source_key=i.source_key ORDER BY c.contract')]
    totals = defaultdict(lambda: Decimal("0"))
    for record in rows:
        key = tuple(record[field] for field in ['entity'])
        value = Decimal(record['amount'])
        if not value.is_finite():
            raise ValueError("数量必须为有限十进制")
        totals[key] += value
    return [dict(zip(['entity'], key), **{'amount': str(value)}) for key, value in sorted(totals.items())]
