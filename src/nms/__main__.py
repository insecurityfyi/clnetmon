"""Command line: nms serve | migrate | collect-once | check-unifi"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from . import db
from .config import get_settings


def _unifi_client(settings):
    from .integrations.unifi.client import UnifiClient
    if not settings.unifi_enabled:
        sys.exit("UniFi is not configured: set NMS_UNIFI_URL and NMS_UNIFI_API_KEY")
    return UnifiClient(settings.unifi_url, settings.unifi_api_key, settings.unifi_site,
                       settings.unifi_verify_tls, settings.unifi_username, settings.unifi_password)


async def _check_unifi() -> int:
    """Probe every endpoint the collector uses and report what the console returns.
    Touches nothing in the database."""
    from .integrations.unifi.client import UnifiError
    failures = 0
    async with _unifi_client(get_settings()) as c:
        try:
            info = await c.integration("info")
            print(f"ok    integration/info       Network application {info.get('applicationVersion')}")
        except UnifiError as e:
            failures += 1
            print(f"FAIL  integration/info       {e}")
        for path, key_fields in (
            ("stat/device", ("radio_table_stats", "port_table", "lldp_table", "uplink")),
            ("stat/sta", ("signal", "ap_mac", "sw_mac")),
            ("rest/user", ()), ("rest/wlanconf", ()), ("rest/networkconf", ()),
            ("stat/routing", ("pfx", "nh")),
        ):
            try:
                data = await c.legacy(path)
                present = [f for f in key_fields if any(f in d for d in data)]
                missing = [f for f in key_fields if f not in present]
                note = f"  missing fields: {', '.join(missing)}" if missing and data else ""
                print(f"ok    {path:<22} {len(data)} records{note}")
            except UnifiError as e:
                failures += 1
                print(f"FAIL  {path:<22} {e}")
    return 1 if failures else 0


async def _collect_once() -> int:
    from . import scheduler
    settings = get_settings()
    pool = await db.create_pool(settings.database_url)
    try:
        await db.migrate(pool)
        ok = True
        for collector in scheduler.build_collectors(settings):
            ok &= await scheduler.run_recorded(pool, collector)
            row = await pool.fetchrow(
                "SELECT ok, counts, warnings, error FROM collector_runs WHERE src = $1 "
                "ORDER BY id DESC LIMIT 1", collector.name)
            print(collector.name, json.dumps(dict(row), default=str))
            await collector.client.aclose()
        return 0 if ok else 1
    finally:
        await pool.close()


async def _migrate() -> int:
    pool = await db.create_pool(get_settings().database_url)
    try:
        applied = await db.migrate(pool)
        print("applied:", ", ".join(applied) if applied else "nothing to do")
        return 0
    finally:
        await pool.close()


def main() -> None:
    p = argparse.ArgumentParser(prog="nms")
    p.add_argument("command", choices=("serve", "migrate", "collect-once", "check-unifi"))
    cmd = p.parse_args().command
    if cmd == "serve":
        import uvicorn
        s = get_settings()
        uvicorn.run("nms.main:app", host=s.api_host, port=s.api_port, log_config=None)
        return
    sys.exit(asyncio.run({"migrate": _migrate, "collect-once": _collect_once,
                          "check-unifi": _check_unifi}[cmd]()))


if __name__ == "__main__":
    main()
