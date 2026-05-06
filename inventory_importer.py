"""
inventory_importer.py
---------------------
Parse REsimpli Inventory CSV exports — properties the team currently OWNS
(or has under contract). The headline metric is "expected profit if every
deal in the current pipeline closes."
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from pathlib import Path


CORE_COLUMNS = [
    "Property ID",
    "Lead Created Date",
    "First Name",
    "Last Name",
    "Phone Number",
    "Email Address",
    "Campaign Name",
    "Lead Source",
    "Project Type",
    "Purchase Date",
    "purchasePrice",
    "Property Street Address",
    "Property City",
    "Property State",
    "Property Zip",
    "Property Status",
    "Sales Price",
    "Sales Date",
    "Owner Mailing Address",
    "Appointment Date",
    "Offer Date",
    "Under Contract Date",
    "Expected Profit",
]


def parse_inventory_csv(file_obj_or_path) -> list[dict]:
    """Parse an Inventory export CSV into a list of dict rows."""
    if isinstance(file_obj_or_path, (str, Path)):
        with open(file_obj_or_path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    elif isinstance(file_obj_or_path, bytes):
        text = file_obj_or_path.decode("utf-8", errors="replace")
        rows = list(csv.DictReader(io.StringIO(text)))
    elif isinstance(file_obj_or_path, str):
        rows = list(csv.DictReader(io.StringIO(file_obj_or_path)))
    else:
        try:
            file_obj_or_path.seek(0)
        except Exception:
            pass
        text = file_obj_or_path.read()
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        rows = list(csv.DictReader(io.StringIO(text)))

    return [
        {k: (v.strip() if isinstance(v, str) else v) for k, v in r.items()}
        for r in rows
    ]


def parse_dollar(s: str) -> float:
    if not s:
        return 0.0
    try:
        return float(str(s).replace("$", "").replace(",", "").strip() or 0)
    except (ValueError, TypeError):
        return 0.0


def summarize(rows: list[dict]) -> dict:
    """Aggregate expected profit by status, project type, etc."""
    by_status = defaultdict(lambda: {"count": 0, "profit": 0.0})
    by_type = defaultdict(lambda: {"count": 0, "profit": 0.0})
    by_source = defaultdict(lambda: {"count": 0, "profit": 0.0})

    total_profit = 0.0
    total_purchase = 0.0
    total_sales = 0.0

    for r in rows:
        profit = parse_dollar(r.get("Expected Profit", ""))
        purchase = parse_dollar(r.get("purchasePrice", ""))
        sales = parse_dollar(r.get("Sales Price", ""))

        total_profit += profit
        total_purchase += purchase
        total_sales += sales

        s = r.get("Property Status", "(unknown)") or "(unknown)"
        by_status[s]["count"] += 1
        by_status[s]["profit"] += profit

        t = r.get("Project Type", "(unknown)") or "(unknown)"
        by_type[t]["count"] += 1
        by_type[t]["profit"] += profit

        src = r.get("Lead Source", "(unknown)") or "(unknown)"
        by_source[src]["count"] += 1
        by_source[src]["profit"] += profit

    # Properties with zero/missing profit (TBD or new acquisitions)
    no_profit = sum(
        1 for r in rows if parse_dollar(r.get("Expected Profit", "")) == 0
    )

    return {
        "total_props": len(rows),
        "total_expected_profit": total_profit,
        "total_purchase": total_purchase,
        "total_sales": total_sales,
        "props_with_profit": len(rows) - no_profit,
        "props_no_profit_yet": no_profit,
        "by_status": dict(sorted(
            by_status.items(), key=lambda x: -x[1]["profit"]
        )),
        "by_project_type": dict(sorted(
            by_type.items(), key=lambda x: -x[1]["profit"]
        )),
        "by_source": dict(sorted(
            by_source.items(), key=lambda x: -x[1]["profit"]
        )),
    }
