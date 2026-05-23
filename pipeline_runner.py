"""
pipeline_runner.py
------------------
Runs the full 4-step acquisitions pipeline (download → parse → skip-trace →
push to sheet) as a SINGLE script that can be spawned as a detached
background process.

Designed to survive Streamlit reruns: the dashboard spawns this with
subprocess.Popen(start_new_session=True), which double-forks the process
away from the Streamlit script's process group. Even if the user closes
the browser, refreshes, or Streamlit reruns its script, this keeps going.

Status is communicated through three files in DATA_DIR:
  pipeline.log    — combined stdout/stderr from all 4 steps
  pipeline.pid    — written on start, removed on clean exit
  pipeline.state  — single-line status: "running:step", "done", "failed:step"

Usage:
    python pipeline_runner.py [--skip-download] [--skip-trace] [--skip-push]
"""

from __future__ import annotations

import argparse
import csv
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()


BASE_DIR = Path(__file__).parent.resolve()
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR)).resolve()

LOG_PATH = DATA_DIR / "pipeline.log"
PID_PATH = DATA_DIR / "pipeline.pid"
STATE_PATH = DATA_DIR / "pipeline.state"

PYTHON = sys.executable


def write_state(s: str) -> None:
    STATE_PATH.write_text(s + "\n")


def log(msg: str) -> None:
    """Append to pipeline.log and to stdout (so we can see it during dev)."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    sys.stdout.write(line)
    sys.stdout.flush()
    with open(LOG_PATH, "a") as f:
        f.write(line)


def run_step(step_name: str, cmd: list[str]) -> int:
    """Run a step, streaming its output to pipeline.log."""
    log(f"━━━ {step_name} ━━━")
    log(f"$ {' '.join(cmd)}")
    write_state(f"running:{step_name}")

    env = {
        **os.environ,
        "PYTHONPATH": str(BASE_DIR),
        "PYTHONUNBUFFERED": "1",
    }
    proc = subprocess.Popen(
        cmd,
        cwd=str(DATA_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    with open(LOG_PATH, "a") as logf:
        assert proc.stdout is not None
        for line in iter(proc.stdout.readline, ""):
            logf.write(line)
            logf.flush()
            sys.stdout.write(line)
            sys.stdout.flush()
        proc.stdout.close()

    rc = proc.wait()
    log(f"{step_name} exit code: {rc}")
    return rc


def count_csv_rows(p: Path) -> int:
    if not p.exists():
        return 0
    with open(p) as f:
        return max(0, sum(1 for _ in csv.reader(f)) - 1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--skip-trace", action="store_true")
    parser.add_argument("--skip-push", action="store_true")
    args = parser.parse_args()

    # Truncate the log on a fresh run so it doesn't grow unbounded.
    LOG_PATH.write_text("")
    PID_PATH.write_text(str(os.getpid()))

    # Trap signals so we leave the state file in a sane state on Ctrl-C / kill.
    def _on_signal(signum, frame):
        write_state(f"failed:signal-{signum}")
        log(f"Received signal {signum}, exiting")
        try:
            PID_PATH.unlink()
        except FileNotFoundError:
            pass
        sys.exit(1)
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    log(f"Pipeline starting (PID {os.getpid()})")
    log(f"DATA_DIR = {DATA_DIR}")

    try:
        # ─── Step 1: Download new PDFs ─────────────────────────────────
        if not args.skip_download:
            rc = run_step(
                "1/4 Download PDFs",
                [PYTHON, str(BASE_DIR / "county_downloader.py")],
            )
            if rc != 0:
                write_state("failed:download")
                log("Pipeline halted at download step")
                return rc

        # ─── Step 2: Parse PDFs ────────────────────────────────────────
        rc = run_step(
            "2/4 Parse PDFs (OCR)",
            [PYTHON, str(BASE_DIR / "main.py"),
             "--input", "./input_pdfs", "--output", "leads.csv"],
        )
        if rc != 0:
            write_state("failed:parse")
            log("Pipeline halted at parse step")
            return rc

        # Decide which CSV to use for the rest
        new_file = None
        for candidate in ("leads_new_equity.csv", "leads_new.csv"):
            p = DATA_DIR / candidate
            if p.exists():
                new_file = candidate
                break

        if not new_file or count_csv_rows(DATA_DIR / new_file) == 0:
            write_state("done:no-new-leads")
            log("Pipeline finished — no NEW leads this run.")
            return 0

        log(f"Found {count_csv_rows(DATA_DIR / new_file)} new lead(s) "
            f"in {new_file}")

        # ─── Step 3: Skip trace ────────────────────────────────────────
        if not args.skip_trace:
            rc = run_step(
                "3/4 Skip trace",
                [PYTHON, str(BASE_DIR / "skipgenie.py"),
                 "--input", new_file, "--output", "leads_new_traced.csv"],
            )
            if rc != 0:
                # Don't halt — we may still want to push partial results
                log("Skip trace had issues; continuing to push step.")

        # ─── Step 4: Push to Google Sheets ─────────────────────────────
        if not args.skip_push:
            rc = run_step(
                "4/4 Push to Google Sheets",
                [PYTHON, "-c",
                 "import csv\n"
                 "from sheets_exporter import export_to_sheets\n"
                 "with open('leads_new_traced.csv') as f:\n"
                 "    records = list(csv.DictReader(f))\n"
                 "export_to_sheets(records)\n"],
            )
            if rc != 0:
                write_state("failed:push")
                log("Pipeline finished steps 1-3 but Sheets push failed")
                return rc

        write_state("done")
        log("✅ Pipeline complete")
        return 0

    finally:
        try:
            PID_PATH.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    sys.exit(main())
