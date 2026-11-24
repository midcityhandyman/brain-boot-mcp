from __future__ import annotations

import json

import pytest

from brain_boot_mcp.__main__ import main
from brain_boot_mcp.config import load_config, open_store
from brain_boot_mcp.service import BrainService
from brain_boot_mcp.store import StoreError
from brain_boot_mcp.store_sqlite import SqliteStore


def test_defaults_need_no_environment():
    config = load_config({})
    assert config.backend == "sqlite"
    assert config.sqlite_path == "brain.db"
    assert config.pack.correction_window_days == 14
    assert config.pack.due_horizon_hours == 24


def test_environment_overrides():
    config = load_config(
        {
            "BRAIN_BOOT_DB": "/tmp/x.db",
            "BRAIN_BOOT_CORRECTION_DAYS": "3",
            "BRAIN_BOOT_CORRECTION_LIMIT": "2",
            "BRAIN_BOOT_DUE_HOURS": "0",
            "BRAIN_BOOT_DUE_LIMIT": "5",
        }
    )
    assert config.sqlite_path == "/tmp/x.db"
    assert (config.pack.correction_window_days, config.pack.correction_limit) == (3, 2)
    assert (config.pack.due_horizon_hours, config.pack.due_limit) == (0, 5)


@pytest.mark.parametrize(
    "env",
    [
        {"BRAIN_BOOT_BACKEND": "mongo"},
        {"BRAIN_BOOT_CORRECTION_DAYS": "many"},
        {"BRAIN_BOOT_DUE_LIMIT": "0"},
    ],
)
def test_bad_environment_is_rejected(env):
    with pytest.raises(StoreError):
        load_config(env)


def test_postgres_backend_requires_a_dsn():
    with pytest.raises(StoreError):
        open_store(load_config({"BRAIN_BOOT_BACKEND": "postgres"}))


def test_open_store_returns_sqlite_by_default(tmp_path):
    store = open_store(load_config({"BRAIN_BOOT_DB": str(tmp_path / "a.db")}))
    assert isinstance(store, SqliteStore)
    store.close()


def test_pack_settings_limit_what_goes_in(clock):
    from brain_boot_mcp.models import PackSettings

    store = SqliteStore(":memory:")
    store.init_schema()
    service = BrainService(store, PackSettings(correction_limit=2, due_horizon_hours=0), clock)
    for n in range(5):
        clock.advance(minutes=1)
        service.add_correction(f"Correction number {n}.")
    pack = service.boot("ops_agent")
    assert [c["text"] for c in pack["recent_corrections"]] == [
        "Correction number 4.",
        "Correction number 3.",
    ]


def test_cli_human_loop(tmp_path, capsys):
    db = str(tmp_path / "brain.db")
    assert main(["--db", db, "seed"]) == 0
    assert json.loads(capsys.readouterr().out)["rules"] == 5
    assert main(["--db", db, "seed"]) == 0
    assert "not seeding twice" in capsys.readouterr().out

    assert main(["--db", db, "boot", "ops_agent"]) == 0
    before = json.loads(capsys.readouterr().out)
    assert before["counts"]["standing_rules"] == 4

    store = SqliteStore(db)
    store.init_schema()
    proposal = BrainService(store).propose_rule("ops_agent", "Read the job notes first.")
    store.close()

    assert main(["--db", db, "proposals", "--status", "pending"]) == 0
    listed = json.loads(capsys.readouterr().out.strip())
    assert listed["proposal_id"] == proposal["proposal_id"]

    assert main(["--db", db, "approve", str(proposal["proposal_id"])]) == 0
    assert "approved proposal" in capsys.readouterr().out
    assert main(["--db", db, "approve", str(proposal["proposal_id"])]) == 1
    assert "already approved" in capsys.readouterr().err

    assert main(["--db", db, "correct", "Speak plainly.", "--agent", "ops_agent"]) == 0
    capsys.readouterr()
    assert main(["--db", db, "boot", "ops_agent"]) == 0
    after = json.loads(capsys.readouterr().out)
    assert after["counts"]["standing_rules"] == 5
    assert after["recent_corrections"][0]["text"] == "Speak plainly."
    assert after["pack_id"] != before["pack_id"]

    assert main(["--db", db, "reject", "999"]) == 1
