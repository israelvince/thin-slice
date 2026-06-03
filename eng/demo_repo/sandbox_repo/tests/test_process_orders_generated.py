import csv
from sandbox_repo import process_orders

def test_compute_customer_total_tmp(tmp_path):
    inp = tmp_path / 'orders.csv'
    out = tmp_path / 'out.csv'
    rows = [
        {'customer_id': 'C1', 'price': '10'},
        {'customer_id': 'C2', 'price': '5'},
        {'customer_id': 'C1', 'price': '3'},
    ]
    with open(inp, 'w', newline='', encoding='utf-8') as fh:
        w = csv.DictWriter(fh, fieldnames=['customer_id', 'price'])
        w.writeheader()
        for r in rows:
            w.writerow(r)
    process_orders.compute_customer_total(str(inp), str(out))
    assert out.exists()
    data = {r['customer_id']: float(r['total_spent'])
            for r in csv.DictReader(open(out))}
    assert data['C1'] == 13.0
    assert data['C2'] == 5.0
