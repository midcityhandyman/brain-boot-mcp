"""Configuration from environment variables. See .env.example for the full list.

There are no secrets in the default setup: the SQLite backend needs only a file
path. The Postgres DSN is the one sensitive value and is read from the
environment only, never from a file in the repository.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from .models import PackSettings
from .store import BrainStore, StoreError

BACKENDS = ("sqlite", "postgres")


@dataclass(frozen=True)
class Config:
    backend: str = "sqlite"
    sqlite_path: str = "brain.db"
    pg_dsn: str = ""
    pack: PackSettings = PackSettings()


def _int(env: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise StoreError(f"{name} must be a whole number, got {raw!r}") from exc
    if not low <= value <= high:
        raise StoreError(f"{name} must be between {low} and {high}, got {value}")
    return value


def load_config(env: Mapping[str, str] | None = None) -> Config:
    source = os.environ if env is None else env
    backend = source.get("BRAIN_BOOT_BACKEND", "sqlite").strip().lower() or "sqlite"
    if backend not in BACKENDS:
        raise StoreError(f"BRAIN_BOOT_BACKEND must be one of {list(BACKENDS)}, got {backend!r}")
    defaults = PackSettings()
    pack = PackSettings(
        correction_window_days=_int(
            source, "BRAIN_BOOT_CORRECTION_DAYS", defaults.correction_window_days, 1, 365
        ),
        correction_limit=_int(
            source, "BRAIN_BOOT_CORRECTION_LIMIT", defaults.correction_limit, 1, 100
        ),
        due_horizon_hours=_int(
            source, "BRAIN_BOOT_DUE_HOURS", defaults.due_horizon_hours, 0, 24 * 30
        ),
        due_limit=_int(source, "BRAIN_BOOT_DUE_LIMIT", defaults.due_limit, 1, 100),
    )
    return Config(
        backend=backend,
        sqlite_path=source.get("BRAIN_BOOT_DB", "brain.db").strip() or "brain.db",
        pg_dsn=source.get("BRAIN_BOOT_PG_DSN", "").strip(),
        pack=pack,
    )


def open_store(config: Config) -> BrainStore:
    """Open the configured backend. Does not create tables; call init_schema()."""
    if config.backend == "postgres":
        if not config.pg_dsn:
            raise StoreError("BRAIN_BOOT_PG_DSN is required when BRAIN_BOOT_BACKEND=postgres")
        from .store_pg import PostgresStore

        return PostgresStore(config.pg_dsn)
    from .store_sqlite import SqliteStore

    return SqliteStore(config.sqlite_path)
