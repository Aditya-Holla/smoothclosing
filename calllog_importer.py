"""
calllog_importer.py
-------------------
Parse the "Exported Call Logs" CSV (REsimpli / RingCentral communication
history) and rank team members by outbound activity — how many calls and
texts each person made.

The key columns are:
  - "Communication Type" — Outgoing/Incoming Call, Outgoing/Incoming SMS/MMS,
    Missed Call, Voice Mail
  - "Caller/Sender"       — the team member who placed the call / sent the text.
    Only OUTBOUND rows carry a sender; inbound rows (a lead calling/texting in,
    missed calls, voicemails) leave it blank, so grouping by a non-empty sender
    naturally isolates agent-initiated activity.
  - "Call Duration"       — HH:MM:SS talk time (00:00:00 for texts)
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from pathlib import Path


def parse_calllog_csv(file_obj_or_path) -> list[dict]:
    """Parse a Call Logs export CSV into a list of dict rows."""
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


def duration_to_seconds(s: str) -> int:
    """Convert an HH:MM:SS (or MM:SS) duration string to seconds."""
    if not s:
        return 0
    parts = str(s).strip().split(":")
    try:
        parts = [int(p) for p in parts]
    except (ValueError, TypeError):
        return 0
    secs = 0
    for p in parts:
        secs = secs * 60 + p
    return secs


def _is_call(comm_type: str) -> bool:
    return "call" in comm_type.lower()


def _is_text(comm_type: str) -> bool:
    t = comm_type.lower()
    return "sms" in t or "mms" in t


def summarize_by_person(rows: list[dict]) -> dict:
    """Rank team members by outbound call + text volume.

    Only rows with a populated "Caller/Sender" count — those are the
    agent-initiated (outbound) communications. Inbound calls/texts, missed
    calls and voicemails have no sender and are reported separately as
    company-wide inbound totals.
    """
    per_person = defaultdict(lambda: {
        "calls": 0,
        "texts": 0,
        "total": 0,
        "talk_seconds": 0,
    })

    inbound_calls = 0
    inbound_texts = 0
    missed_calls = 0

    for r in rows:
        comm = (r.get("Communication Type", "") or "").strip()
        sender = (r.get("Caller/Sender", "") or "").strip()

        if not sender:
            # Inbound / unattributed activity
            if "missed" in comm.lower():
                missed_calls += 1
            elif _is_call(comm):
                inbound_calls += 1
            elif _is_text(comm):
                inbound_texts += 1
            continue

        p = per_person[sender]
        if _is_call(comm):
            p["calls"] += 1
            p["total"] += 1
            p["talk_seconds"] += duration_to_seconds(
                r.get("Call Duration", "")
            )
        elif _is_text(comm):
            p["texts"] += 1
            p["total"] += 1

    ranked = sorted(
        per_person.items(),
        key=lambda kv: (-kv[1]["total"], -kv[1]["calls"], kv[0]),
    )

    return {
        "ranked": ranked,
        "total_calls": sum(d["calls"] for _, d in ranked),
        "total_texts": sum(d["texts"] for _, d in ranked),
        "total_activity": sum(d["total"] for _, d in ranked),
        "total_talk_seconds": sum(d["talk_seconds"] for _, d in ranked),
        "people": len(ranked),
        "inbound_calls": inbound_calls,
        "inbound_texts": inbound_texts,
        "missed_calls": missed_calls,
    }
