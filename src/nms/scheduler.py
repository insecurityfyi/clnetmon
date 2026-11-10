"""Runs each enabled collector on its own interval and records every run."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import asyncpg

from .config import Settings
from .integrations.unifi.client import UnifiClient
from .integrations.snmp.collector import SnmpCollector
from .integrations.unifi.collector import UnifiCollector

log = logging.getLogger(__name__)

METRIC_TABLES = ("device_metrics", "radio_metrics", "port_metrics", "client_metrics",
                 "printer_metrics", "printer_supplies")


def build_collectors(settings: Settings) -> list:
    collectors = []
    if settings.unifi_enabled:
        collectors.append(UnifiCollector(
            UnifiClient(
                settings.unifi_url, settings.unifi_api_key, settings.unifi_site,
                settings.unifi_verify_tls, settings.unifi_username, settings.unifi_password,
            ),
            interval=settings.unifi_interval,
        ))
    if settings.snmp_enabled:
        collectors.append(SnmpCollector(
            settings.snmp_community_list, settings._list(settings.snmp_targets),
            settings._list(settings.snmp_discovery), interval=settings.snmp_interval,
            discovery_interval=settings.snmp_discovery_interval, timeout=settings.snmp_timeout,
        ))
    return collectors


async def run_recorded(pool: asyncpg.Pool, collector) -> bool:
    started = datetime.now(timezone.utc)
    counts, warnings, error = None, None, None
    try:
        counts, warnings = await collector.run_once(pool)
    except Exception as e:  # a failed poll must never stop the loop
        error = f"{type(e).__name__}: {e}"
        log.error("%s collection failed: %s", collector.name, error)
    else:
        log.info("%s collected %s%s", collector.name, counts,
                 f" warnings={warnings}" if warnings else "")
    await pool.execute(
        "INSERT INTO collector_runs (src, started_at, finished_at, ok, counts, warnings, error) "
        "VALUES ($1, $2, now(), $3, $4, $5, $6)",
        collector.name, started, error is None, counts, warnings or None, error)
    return error is None


async def _loop(pool: asyncpg.Pool, collector) -> None:
    while True:
        t0 = asyncio.get_running_loop().time()
        await run_recorded(pool, collector)
        elapsed = asyncio.get_running_loop().time() - t0
        await asyncio.sleep(max(5.0, collector.interval - elapsed))


async def _retention(pool: asyncpg.Pool, days: int) -> None:
    while True:
        try:
            for t in METRIC_TABLES:
                await pool.execute(
                    f"DELETE FROM {t} WHERE time < now() - make_interval(days => $1)", days)
            await pool.execute(
                "DELETE FROM collector_runs WHERE started_at < now() - make_interval(days => $1)",
                days)
        except Exception as e:
            log.error("retention failed: %s", e)
        await asyncio.sleep(6 * 3600)


def start(pool: asyncpg.Pool, settings: Settings) -> list[asyncio.Task]:
    collectors = build_collectors(settings)
    if not collectors:
        log.warning("no collectors enabled; configure UniFi and/or SNMP in the env file")
    tasks = [asyncio.create_task(_loop(pool, c), name=f"collector-{c.name}") for c in collectors]
    tasks.append(asyncio.create_task(_retention(pool, settings.retention_days), name="retention"))
    return tasks
