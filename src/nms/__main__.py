"""Command line: nms serve | migrate | collect-once | check-unifi | check-snmp"""
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


async def _check_snmp() -> int:
    """Probe the static targets and sweep the discovery ranges; print who answers.
    Writes nothing to the database."""
    from .integrations.snmp.collector import SnmpCollector
    s = get_settings()
    if not s.snmp_enabled:
        sys.exit("SNMP is not configured: set NMS_SNMP_COMMUNITIES and NMS_SNMP_TARGETS "
                 "and/or NMS_SNMP_DISCOVERY")
    col = SnmpCollector(s.snmp_community_list, s._list(s.snmp_targets),
                        s._list(s.snmp_discovery), timeout=s.snmp_timeout)
    pool = None
    if any(e.lower() == "auto" for e in col.discovery):
        try:
            pool = await db.create_pool(s.database_url)
        except Exception as e:
            print(f"note: cannot read learned subnets for 'auto' ({e})")
    try:
        swept, warnings = await col.discovery_hosts(pool)
        for w in warnings:
            print("note:", w)
        hosts = list(dict.fromkeys(col.static + swept))
        print(f"probing {len(hosts)} address(es) with {len(col.communities)} community string(s)...")
        found = await col.sweep(hosts)
        for h in hosts:
            if h in found:
                print(f"ok    {h:<18} {found[h][1] or ''}")
            elif h in col.static:
                print(f"FAIL  {h:<18} no answer")
        print(f"{len(found)} device(s) answered")
        return 0 if all(h in found for h in col.static) else 1
    finally:
        await col.aclose()
        if pool is not None:
            await pool.close()


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
            await collector.aclose()
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
    p.add_argument("command", choices=("serve", "migrate", "collect-once", "check-unifi", "check-snmp"))
    cmd = p.parse_args().command
    if cmd == "serve":
        import uvicorn
        s = get_settings()
        uvicorn.run("nms.main:app", host=s.api_host, port=s.api_port, log_config=None)
        return
    sys.exit(asyncio.run({"migrate": _migrate, "collect-once": _collect_once,
                          "check-unifi": _check_unifi, "check-snmp": _check_snmp}[cmd]()))


if __name__ == "__main__":
    main()
