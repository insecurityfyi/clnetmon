"""PostgreSQL access: pool, migrations and small bulk-write helpers."""
from __future__ import annotations

import json
import logging
from importlib import resources
from typing import Any, Iterable, Sequence

import asyncpg

log = logging.getLogger(__name__)


async def _init_conn(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


async def create_pool(dsn: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn, min_size=1, max_size=5, init=_init_conn)


async def migrate(pool: asyncpg.Pool) -> list[str]:
    """Apply any migrations/*.sql not yet recorded, in filename order."""
    applied: list[str] = []
    files = sorted(
        (f for f in resources.files("nms.migrations").iterdir() if f.name.endswith(".sql")),
        key=lambda f: f.name,
    )
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock(727001)")
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            done = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
            for f in files:
                if f.name in done:
                    continue
                async with conn.transaction():
                    await conn.execute(f.read_text())
                    await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", f.name)
                applied.append(f.name)
                log.info("applied migration %s", f.name)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(727001)")
    return applied


def _cols(rows: Sequence[dict[str, Any]]) -> list[str]:
    return list(rows[0].keys())


async def insert_rows(conn: asyncpg.Connection, table: str, rows: Sequence[dict[str, Any]]) -> None:
    if not rows:
        return
    cols = _cols(rows)
    sql = "INSERT INTO {} ({}) VALUES ({})".format(
        table, ", ".join(cols), ", ".join(f"${i + 1}" for i in range(len(cols)))
    )
    await conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])


async def upsert_rows(
    conn: asyncpg.Connection,
    table: str,
    rows: Sequence[dict[str, Any]],
    key: Iterable[str],
    overrides: dict[str, str] | None = None,
) -> None:
    """INSERT ... ON CONFLICT (key) DO UPDATE. "overrides" maps a column to a custom
    SQL expression for the update (may reference EXCLUDED.col and <table>.col)."""
    if not rows:
        return
    cols = _cols(rows)
    key = list(key)
    overrides = overrides or {}
    updates = [c for c in cols if c not in key]
    sql = "INSERT INTO {} ({}) VALUES ({}) ON CONFLICT ({}) DO UPDATE SET {}".format(
        table,
        ", ".join(cols),
        ", ".join(f"${i + 1}" for i in range(len(cols))),
        ", ".join(key),
        ", ".join(f"{c} = {overrides.get(c, 'EXCLUDED.' + c)}" for c in updates),
    )
    await conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
