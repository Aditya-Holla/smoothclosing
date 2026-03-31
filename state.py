"""
state.py
--------
Persistent pipeline state management.
Tracks which PDFs have been downloaded/processed and which leads
are already in the spreadsheet, so the pipeline only handles new data.

State file: pipeline_state.json (commit to git so it works on any machine).
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_FILE = Path(__file__).parent / "pipeline_state.json"

DEFAULT_STATE = {
    "downloaded_pdfs": {},
    "processed_pdfs": {},
    "known_lead_keys": [],
}


def load_state(path: Path = STATE_FILE) -> dict:
    """Load pipeline state from JSON file. Returns default if missing."""
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)
            # Ensure all keys exist (forward-compat)
            for key, default in DEFAULT_STATE.items():
                state.setdefault(key, default)
            logger.info(f"State loaded: {len(state['downloaded_pdfs'])} downloaded, "
                        f"{len(state['processed_pdfs'])} processed, "
                        f"{len(state['known_lead_keys'])} known leads")
            return state
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Corrupt state file ({e}), starting fresh.")
            return _copy_default()
    else:
        logger.info("No state file found — starting fresh.")
        return _copy_default()


def save_state(state: dict, path: Path = STATE_FILE) -> None:
    """Atomically write state to JSON (write tmp, then rename)."""
    tmp_path = path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)
    logger.debug("State saved.")


def get_known_lead_keys(state: dict) -> set[tuple]:
    """Return the set of (owner_name_lower, property_address_lower) already exported."""
    return {tuple(pair) for pair in state.get("known_lead_keys", [])}


def add_known_lead_keys(state: dict, new_keys: set[tuple]) -> None:
    """Merge new lead keys into state."""
    existing = get_known_lead_keys(state)
    merged = existing | new_keys
    state["known_lead_keys"] = sorted([list(k) for k in merged])


def mark_downloaded(state: dict, rel_key: str, url: str, size_bytes: int) -> None:
    """Record a PDF as downloaded."""
    state["downloaded_pdfs"][rel_key] = {
        "url": url,
        "downloaded_at": datetime.now().isoformat(),
        "size_bytes": size_bytes,
    }


def mark_processed(state: dict, rel_key: str, records_extracted: int) -> None:
    """Record a PDF as processed."""
    state["processed_pdfs"][rel_key] = {
        "processed_at": datetime.now().isoformat(),
        "records_extracted": records_extracted,
    }


def is_downloaded(state: dict, rel_key: str) -> bool:
    return rel_key in state.get("downloaded_pdfs", {})


def is_processed(state: dict, rel_key: str) -> bool:
    return rel_key in state.get("processed_pdfs", {})


def _copy_default() -> dict:
    return json.loads(json.dumps(DEFAULT_STATE))
