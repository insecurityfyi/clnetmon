"""SNMP collector: discovery sweep + per-device polling into PostgreSQL."""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

import asyncpg

from ... import db
from . import mib

log = logging.getLogger(__name__)

MIN_PREFIX = 22          # never sweep anything larger than a /22 (1022 hosts) per entry
PROBE_CONCURRENCY = 64
POLL_CONCURRENCY = 8
DEVICE_TIMEOUT = 90.0


def expand_cidrs(entries: list[str]) -> tuple[list[str], list[str]]:
    """CIDR strings -> (host addresses, warnings)."""
    hosts: list[str] = []
    warnings: list[str] = []
    for e in entries:
        try:
            net = ipaddress.ip_network(e, strict=False)
        except ValueError:
            warnings.append(f"discovery: '{e}' is not a valid network")
            continue
        if net.version != 4:
            warnings.append(f"discovery: {net} skipped (IPv4 only)")
        elif net.prefixlen < MIN_PREFIX:
            warnings.append(f"discovery: {net} skipped (larger than /{MIN_PREFIX})")
        else:
            hosts.extend(str(h) for h in (net.hosts() if net.prefixlen < 31 else net))
    return list(dict.fromkeys(hosts)), warnings


class SnmpCollector:
    name = mib.SRC

    def __init__(self, communities: list[str], targets: list[str], discovery: list[str],
                 interval: int = 120, discovery_interval: int = 3600, timeout: float = 2.0,
                 session_factory: Callable[..., Any] | None = None) -> None:
        self.communities = communities
        self.static = list(dict.fromkeys(targets))
        self.discovery = discovery
        self.interval = interval
        self.discovery_interval = discovery_interval
        self.timeout = timeout
        self._factory = session_factory
        self._engine = None
        self._last_discovery = 0.0

    # ------------------------------------------------------------------ sessions
    def _session(self, host: str, community: str, timeout: float, retries: int):
        if self._factory:
            return self._factory(host, community, timeout=timeout, retries=retries)
        from pysnmp.hlapi.v3arch.asyncio import SnmpEngine
        from .client import SnmpSession
        if self._engine is None:
            self._engine = SnmpEngine()
        return SnmpSession(self._engine, host, community, timeout=timeout, retries=retries)

    async def aclose(self) -> None:
        if self._engine is not None:
            try:
                self._engine.close_dispatcher()
            except Exception:
                pass
            self._engine = None

    # ------------------------------------------------------------------ discovery
    async def probe(self, host: str, timeout: float = 1.0) -> tuple[int, str | None] | None:
        """Which configured community does this host answer to? -> (index, sysName)."""
        for i, community in enumerate(self.communities):
            try:
                got = await self._session(host, community, timeout, 0).get(mib.SYS_NAME,
                                                                           mib.SYS_OBJECT_ID)
            except Exception:
                continue
            if got:
                return i, mib.text(got.get(mib.SYS_NAME))
        return None

    async def discovery_hosts(self, pool: asyncpg.Pool | None) -> tuple[list[str], list[str]]:
        cidrs = [e for e in self.discovery if e.lower() != "auto"]
        if pool is not None and any(e.lower() == "auto" for e in self.discovery):
            cidrs += [r["subnet"] for r in await pool.fetch(
                "SELECT DISTINCT subnet FROM networks "
                "WHERE subnet IS NOT NULL AND COALESCE(purpose, '') <> 'wan'")]
        return expand_cidrs(cidrs)

    async def sweep(self, hosts: list[str]) -> dict[str, tuple[int, str | None]]:
        sem = asyncio.Semaphore(PROBE_CONCURRENCY)

        async def one(h: str):
            async with sem:
                return h, await self.probe(h)

        return {h: r for h, r in await asyncio.gather(*(one(h) for h in hosts)) if r}

    async def refresh_targets(self, pool: asyncpg.Pool) -> list[str]:
        """Probe static targets that have no working community yet, and sweep the
        discovery ranges for new responders."""
        known = {r["host"]: r for r in await pool.fetch("SELECT * FROM snmp_targets")}
        swept, warnings = await self.discovery_hosts(pool)
        todo = [h for h in self.static if known.get(h) is None or known[h]["community_ref"] is None]
        todo += [h for h in swept if h not in known and h not in todo]
        found = await self.sweep(todo)
        async with pool.acquire() as conn, conn.transaction():
            for h in self.static:
                await conn.execute(
                    "INSERT INTO snmp_targets (host, origin, last_error) VALUES ($1, 'static', $2) "
                    "ON CONFLICT (host) DO UPDATE SET origin = 'static'",
                    h, None if h in found else "no answer to any configured community")
            for h, (ref, sys_name) in found.items():
                await conn.execute(
                    "INSERT INTO snmp_targets (host, origin, community_ref, sys_name) "
                    "VALUES ($1, $2, $3, $4) ON CONFLICT (host) DO UPDATE SET "
                    "community_ref = EXCLUDED.community_ref, sys_name = EXCLUDED.sys_name, "
                    "last_error = NULL",
                    h, "static" if h in self.static else "discovered", ref, sys_name)
        new = [h for h in found if h not in known]
        if new:
            log.info("snmp discovery: %d new device(s): %s", len(new), ", ".join(new))
        return warnings

    # ------------------------------------------------------------------ polling
    async def _poll(self, host: str, ref: int, now: datetime, known: frozenset[str]):
        if ref >= len(self.communities):
            return host, None, "community no longer configured"
        try:
            snap = await asyncio.wait_for(
                mib.poll_device(self._session(host, self.communities[ref], self.timeout, 1),
                                host, now, known),
                DEVICE_TIMEOUT)
            return host, snap, None
        except asyncio.TimeoutError:
            return host, None, f"gave up after {DEVICE_TIMEOUT:.0f}s"
        except Exception as e:
            return host, None, f"{type(e).__name__}: {e}"

    async def run_once(self, pool: asyncpg.Pool) -> tuple[dict[str, int], list[str]]:
        warnings: list[str] = []
        if time.monotonic() - self._last_discovery >= self.discovery_interval or not self._last_discovery:
            warnings += await self.refresh_targets(pool)
            self._last_discovery = time.monotonic()

        now = datetime.now(timezone.utc)
        targets = await pool.fetch(
            "SELECT host, community_ref FROM snmp_targets "
            "WHERE community_ref IS NOT NULL AND alias_of IS NULL ORDER BY host")
        known = frozenset(r["mac"] for r in await pool.fetch(
            "SELECT mac FROM devices WHERE src <> $1 UNION SELECT mac FROM clients", self.name))

        sem = asyncio.Semaphore(POLL_CONCURRENCY)

        async def one(t):
            async with sem:
                return await self._poll(t["host"], t["community_ref"], now, known)

        results = await asyncio.gather(*(one(t) for t in targets))

        counts = dict.fromkeys(("devices", "offline", "ports", "links", "routes", "arp", "fdb",
                                "printers"), 0)
        seen: dict[str, str] = {}
        async with pool.acquire() as conn, conn.transaction():
            for host, snap, error in results:
                if snap is None:
                    counts["offline"] += 1
                    warnings.append(f"{host}: {error}")
                    await conn.execute(
                        "UPDATE snmp_targets SET last_error = $2 WHERE host = $1", host, error)
                    await conn.execute(
                        "UPDATE devices SET state = 'offline', updated_at = $3 "
                        "WHERE src = $1 AND ip = $2", self.name, host, now)
                    continue
                dev_id = snap["device"]["mac"]
                if dev_id in seen:  # second address of a device we already have
                    await conn.execute(
                        "UPDATE snmp_targets SET alias_of = $2, device_mac = $3, last_ok = $4, "
                        "last_error = NULL WHERE host = $1", host, seen[dev_id], dev_id, now)
                    continue
                seen[dev_id] = host
                warnings += snap["warnings"]
                await self._store(conn, host, snap, now)
                counts["devices"] += 1
                counts["printers"] += bool(snap["printer_metrics"])
                for k, key in (("ports", "ports"), ("links", "links"), ("routes", "routes"),
                               ("arp", "arp"), ("fdb", "fdb")):
                    counts[k] += len(snap[key])
            await db.compute_port_rates(conn, self.name, now)
            for table in ("topology_links", "routes", "arp_entries", "fdb_entries"):
                await conn.execute(
                    f"DELETE FROM {table} WHERE src = $1 AND last_seen < now() - interval '1 day'",
                    self.name)
        return counts, warnings

    async def _store(self, conn: asyncpg.Connection, host: str, snap: dict, now: datetime) -> None:
        dev = snap["device"]
        # the id can change (e.g. once UniFi knows the box): drop the superseded row
        await conn.execute("DELETE FROM devices WHERE src = $1 AND ip = $2 AND mac <> $3",
                           self.name, host, dev["mac"])
        await db.upsert_rows(conn, "devices", [dev], ("src", "mac"))
        await db.insert_rows(conn, "device_metrics", [snap["device_metrics"]])
        await db.insert_rows(conn, "port_metrics", snap["ports"])
        await db.upsert_rows(conn, "topology_links", snap["links"],
                             ("src", "kind", "a_id", "a_port", "b_id", "b_port"))
        await db.upsert_rows(conn, "routes", snap["routes"],
                             ("src", "device_mac", "prefix", "next_hop", "interface"))
        await db.upsert_rows(conn, "arp_entries", snap["arp"], ("src", "device_mac", "ip"))
        await db.upsert_rows(conn, "fdb_entries", snap["fdb"],
                             ("src", "device_mac", "mac", "vlan"))
        await db.insert_rows(conn, "printer_metrics", snap["printer_metrics"])
        await db.insert_rows(conn, "printer_supplies", snap["printer_supplies"])
        await conn.execute(
            "UPDATE snmp_targets SET device_mac = $2, sys_name = $3, last_ok = $4, "
            "last_error = NULL WHERE host = $1", host, dev["mac"], dev["name"], now)
