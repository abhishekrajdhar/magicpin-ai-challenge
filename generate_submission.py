from __future__ import annotations

import json
import random
from pathlib import Path

from bot import compose
from dataset.generate_dataset import SEED, expand_customers, expand_merchants, expand_triggers, load_seeds


ROOT = Path(__file__).resolve().parent
DATASET_DIR = ROOT / "dataset"
OUT = ROOT / "submission.jsonl"


def main() -> None:
    rnd = random.Random(SEED)
    categories, merchant_seeds, customer_seeds, trigger_seeds = load_seeds(DATASET_DIR)
    merchants = {m["merchant_id"]: m for m in expand_merchants(merchant_seeds, rnd)}
    customers_list = expand_customers(customer_seeds, list(merchants.values()), rnd)
    customers = {c["customer_id"]: c for c in customers_list}
    triggers = expand_triggers(trigger_seeds, list(merchants.values()), customers_list, rnd)

    pairs = []
    by_kind: dict[str, list[dict]] = {}
    for trigger in triggers:
        by_kind.setdefault(trigger["kind"], []).append(trigger)
    for kind in sorted(by_kind):
        for trigger in by_kind[kind][:2]:
            pairs.append(trigger)
            if len(pairs) >= 30:
                break
        if len(pairs) >= 30:
            break

    with OUT.open("w", encoding="utf-8") as fh:
        for idx, trigger in enumerate(pairs, start=1):
            merchant = merchants[trigger["merchant_id"]]
            category = categories[merchant["category_slug"]]
            customer = customers.get(trigger.get("customer_id")) if trigger.get("customer_id") else None
            message = compose(category, merchant, trigger, customer)
            record = {"test_id": f"T{idx:02d}", **message}
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"Wrote {len(pairs)} lines to {OUT}")


if __name__ == "__main__":
    main()

