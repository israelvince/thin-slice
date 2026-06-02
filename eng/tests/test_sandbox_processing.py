import os
from eng.sandbox_repo.process_orders import compute_customer_total


def test_compute_customer_total(tmp_path):
    # create a small CSV
    csv_file = tmp_path / "orders.csv"
    csv_file.write_text("customer_id,price\n1,10.0\n2,20.5\n1,5.0\n")
    out_file = tmp_path / "out.csv"
    compute_customer_total(str(csv_file), str(out_file))
    assert out_file.exists()
    text = out_file.read_text()
    assert 'customer_id' in text
