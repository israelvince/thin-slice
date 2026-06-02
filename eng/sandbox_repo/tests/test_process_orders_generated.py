# Auto-generated unit test for process_orders.py
import os
import csv
from sandbox_repo import process_orders


def test_compute_customer_total_tmp(tmp_path):
    # create a tiny input CSV
    input_csv = tmp_path / "orders_small.csv"
    output_csv = tmp_path / "out.csv"
    rows = [
        {"customer_id": "C1", "price": "10"},
        {"customer_id": "C2", "price": "5"},
        {"customer_id": "C1", "price": "3"},
    ]
    with open(input_csv, 'w', newline='', encoding='utf-8') as fh:
        writer = csv.DictWriter(fh, fieldnames=["customer_id", "price"])
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

    process_orders.compute_customer_total(str(input_csv), str(output_csv))

    assert os.path.exists(output_csv)
    with open(output_csv, newline='', encoding='utf-8') as fh:
        reader = csv.DictReader(fh)
        data = {r['customer_id']: float(r['total_spent']) for r in reader}
    assert data.get('C1') == 13.0
    assert data.get('C2') == 5.0
