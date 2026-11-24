from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_demo_runs_offline_and_matches_the_captured_story():
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    done = subprocess.run(
        [sys.executable, str(ROOT / "demo" / "run_demo.py")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        env=env,
    )
    assert done.returncode == 0, done.stderr
    out = done.stdout
    assert "demo complete" in out
    for expected in ("UNCHANGED", "CHANGED", "UNKNOWN_PACK", "duplicate_of_rule", "is_error True"):
        assert expected in out

    # Pack ids are content addressed, so the saved capture must show the same ids.
    saved = (ROOT / "demo" / "output.txt").read_text(encoding="utf-8")
    live_ids = [line for line in out.splitlines() if "pack_id" in line]
    saved_ids = [line for line in saved.splitlines() if "pack_id" in line]
    assert live_ids and live_ids == saved_ids
