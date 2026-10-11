"""Drop rows older than N days from the paper CSV, keeping the header.

  python prune_csv.py paper_trades.csv 30
"""
import csv
import os
import sys
from datetime import datetime, timedelta, timezone

path, days = sys.argv[1], int(sys.argv[2])
if not os.path.exists(path):
    sys.exit(0)

with open(path, newline="") as f:
    rows = list(csv.reader(f))
if not rows:
    sys.exit(0)

header, body = rows[0], rows[1:]
cutoff = datetime.now(timezone.utc) - timedelta(days=days)
kept = [r for r in body if datetime.fromisoformat(r[0]) >= cutoff]
print(f"pruned {len(body) - len(kept)} rows older than {days} days, kept {len(kept)}")

if len(kept) != len(body):
    with open(path, "w", newline="") as f:
        csv.writer(f).writerows([header] + kept)
