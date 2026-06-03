import csv
import argparse
import os
from collections import defaultdict
from typing import Optional


def _safe_float(x):
    try:
        return float(x)
    except Exception:
        return 0.0


def compute_customer_total(input_csv: str, output_csv: str, customer_id_col: str = 'customer_id', top_n: Optional[int] = 100):
    """
    Compute total spend per customer.

    If the matching order_items file exists (by replacing 'orders' with 'order_items'
    in the input filename), join order_items to orders to compute price * quantity
    per order and then sum per customer. Otherwise, fall back to reading a price
    column directly from the input orders file.
    """
    totals = defaultdict(float)

    # Determine base directory and possible order_items file
    base_dir = os.path.dirname(input_csv) or '.'
    input_name = os.path.basename(input_csv)

    # If user passed a directory, try to find the orders file inside
    orders_path = input_csv
    if os.path.isdir(input_csv):
        # prefer files with 'orders' in name
        cand = [f for f in os.listdir(input_csv) if 'order' in f and f.endswith('.csv')]
        if not cand:
            raise FileNotFoundError(f"No orders CSV found in directory {input_csv}")
        # prefer the one with 'orders' substring
        orders_path = os.path.join(input_csv, sorted(cand, key=lambda x: ('orders' not in x, x))[0])

    # detect order_items file (common patterns)
    possible_items = []
    # direct sibling by replacing 'orders' with 'order_items'
    if 'orders' in input_name:
        possible_items.append(os.path.join(base_dir, input_name.replace('orders', 'order_items')))
    # other common filename
    possible_items.append(os.path.join(base_dir, 'olist_order_items_dataset.csv'))
    possible_items.append(os.path.join(base_dir, 'order_items.csv'))
    possible_items.append(os.path.join(base_dir, 'order-item.csv'))

    items_file = None
    for p in possible_items:
        if p and os.path.exists(p):
            items_file = p
            break

    if items_file:
        # Build order -> total from items
        order_totals = defaultdict(float)
        with open(items_file, newline='', encoding='utf-8') as fh:
            reader = csv.DictReader(fh)
            for r in reader:
                oid = r.get('order_id') or r.get('order_id')
                # price usually exists in order_items
                price = _safe_float(r.get('price') or r.get('item_price') or r.get('payment_value') or 0)
                # quantity may be present under several names; default to 1
                qty = _safe_float(r.get('quantity') or r.get('qty') or r.get('order_item_id') and 1 or 1)
                order_totals[oid] += price * qty

        # Map orders to customers and aggregate
        with open(orders_path, newline='', encoding='utf-8') as fh:
            reader = csv.DictReader(fh)
            for r in reader:
                oid = r.get('order_id')
                cid = r.get(customer_id_col) or r.get('customer_id') or r.get('buyer_id')
                if not cid:
                    continue
                totals[cid] += order_totals.get(oid, 0.0)
    else:
        # Fallback: read price field directly from orders file
        with open(orders_path, newline='', encoding='utf-8') as fh:
            reader = csv.DictReader(fh)
            for r in reader:
                cid = r.get(customer_id_col) or r.get('customer_id') or r.get('buyer_id')
                val = _safe_float(r.get('price') or r.get('payment_value') or 0)
                if cid:
                    totals[cid] += val

    # ensure output directory exists
    out_dir = os.path.dirname(output_csv)
    if out_dir and not os.path.exists(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    # write output (top N customers)
    with open(output_csv, 'w', newline='', encoding='utf-8') as outfh:
        writer = csv.writer(outfh)
        writer.writerow(['customer_id', 'total_spent'])
        limit = top_n if top_n is not None else len(totals)
        for cid, total in sorted(totals.items(), key=lambda x: -x[1])[:limit]:
            writer.writerow([cid, f"{total:.2f}"])


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--input', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--top', type=int, default=100, help='Number of top customers to write')
    args = p.parse_args()
    compute_customer_total(args.input, args.output, top_n=args.top)


if __name__ == '__main__':
    main()
