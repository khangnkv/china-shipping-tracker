"""One-off: merge duplicate `customers` rows that share a phone number.

Before this fix, every new order created a brand-new customer row (see
new_order() in app.py), so a repeat customer could end up with several
customer records instead of one parent record with many orders. This
script consolidates those duplicates onto a single surviving row per
phone number, repointing orders.customer_id and preserving line_user_id
if exactly one of the duplicates has it set.

Run once, manually, from /opt/shipping:
    venv/bin/python scripts/merge_customers_by_phone.py            # dry run (default)
    venv/bin/python scripts/merge_customers_by_phone.py --apply    # actually write changes

Always back up tracker.db first:
    sqlite3 tracker.db ".backup backups/tracker-pre-merge-$(date +%F_%H%M%S).db"
"""
import argparse
import os
import re
import sqlite3
import sys

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "tracker.db")


def normalize_phone(phone):
    digits = re.sub(r"\D", "", phone or "")
    return digits or None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args()

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    customers = db.execute("SELECT * FROM customers").fetchall()
    by_phone = {}
    for c in customers:
        key = c["phone_normalized"] or normalize_phone(c["phone"])
        if not key:
            continue  # nothing to merge for customers with no phone
        by_phone.setdefault(key, []).append(c)

    duplicate_groups = {k: v for k, v in by_phone.items() if len(v) > 1}
    if not duplicate_groups:
        print("No duplicate customers found by phone number -- nothing to do.")
        return

    total_orders_moved = 0
    total_customers_removed = 0

    for phone, group in duplicate_groups.items():
        # Prefer a row that already has line_user_id set as the survivor;
        # otherwise keep the earliest-created row.
        linked = [c for c in group if c["line_user_id"]]
        if len(linked) > 1:
            print(f"SKIP {phone}: multiple rows already have different line_user_id "
                  f"set ({[c['id'] for c in linked]}) -- resolve manually.")
            continue
        survivor = linked[0] if linked else sorted(group, key=lambda c: c["created_at"])[0]
        losers = [c for c in group if c["id"] != survivor["id"]]

        print(f"\nPhone {phone}: keeping customer id={survivor['id']} ({survivor['name']!r}), "
              f"merging {[c['id'] for c in losers]}")

        for loser in losers:
            orders = db.execute("SELECT id FROM orders WHERE customer_id = ?", (loser["id"],)).fetchall()
            print(f"  - customer {loser['id']} ({loser['name']!r}): {len(orders)} order(s) -> customer {survivor['id']}")
            total_orders_moved += len(orders)
            if args.apply:
                db.execute("UPDATE orders SET customer_id = ? WHERE customer_id = ?", (survivor["id"], loser["id"]))
                db.execute("DELETE FROM customers WHERE id = ?", (loser["id"],))
            total_customers_removed += 1

    if args.apply:
        db.commit()
        print(f"\nApplied: moved {total_orders_moved} order(s), removed {total_customers_removed} duplicate customer(s).")
    else:
        print(f"\nDry run: would move {total_orders_moved} order(s), remove {total_customers_removed} duplicate customer(s).")
        print("Re-run with --apply to write these changes.")

    db.close()


if __name__ == "__main__":
    main()
