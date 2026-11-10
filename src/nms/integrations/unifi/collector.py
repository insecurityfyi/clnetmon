"""Polls a UniFi OS console and writes one snapshot into PostgreSQL."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import asyncpg

from ... import db
from . import normalize as nz
from .client import UnifiClient, UnifiError

log = logging.getLogger(__name__)

# For clients that are not connected right now, keep the last attachment we saw.
_STICKY = ("ip", "ap_mac", "sw_mac", "sw_port", "essid", "bssid", "network", "vlan",
           "hostname", "name", "display_name", "identity", "oui", "os_name", "dev_vendor",
           "dev_family", "is_wired", "is_guest", "fixed_ip", "note", "first_seen", "last_seen")
_CLIENT_OVERRIDES = {
    c: f"CASE WHEN EXCLUDED.connected THEN EXCLUDED.{c} ELSE COALESCE(EXCLUDED.{c}, clients.{c}) END"
    for c in ("ip", "ap_mac", "sw_mac", "sw_port", "essid", "bssid", "network", "vlan")
} | {
    c: f"COALESCE(EXCLUDED.{c}, clients.{c})"
    for c in _STICKY if c not in ("ip", "ap_mac", "sw_mac", "sw_port", "essid", "bssid",
                                  "network", "vlan")
} | {"raw": "COALESCE(EXCLUDED.raw, clients.raw)"}


class UnifiCollector:
    name = nz.SRC

    def __init__(self, client: UnifiClient, interval: int = 60, known_every: int = 10) -> None:
        self.client = client
        self.interval = interval
        self.site = client.site
        self._known_every = known_every
        self._cycle = 0

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _optional(self, path: str, warnings: list[str]) -> list[dict[str, Any]]:
        try:
            return await self.client.legacy(path)
        except UnifiError as e:
            warnings.append(str(e))
            return []

    async def fetch(self) -> dict[str, Any]:
        """Pull everything for one cycle. Returns normalized row lists plus warnings."""
        now = datetime.now(timezone.utc)
        warnings: list[str] = []
        c = self.client
        snap: dict[str, Any] = {"now": now, "warnings": warnings, "mode": "legacy"}

        try:
            raw_devices = await c.legacy("stat/device")
            raw_active = await c.legacy("stat/sta")
        except UnifiError as e:
            # Detailed endpoints unavailable: fall back to the official integration API,
            # which gives inventory and attachment but no RF/port/LLDP/routing detail.
            warnings.append(f"legacy API unavailable ({e}); using integration API only")
            return await self._fetch_integration(snap)

        dev = nz.devices(raw_devices, self.site, now)
        want_known = self._cycle % self._known_every == 0
        raw_known = await self._optional("rest/user", warnings) if want_known else []
        cli = nz.clients(raw_active, raw_known, self.site, now)

        snap.update(dev)
        snap["clients"] = cli["clients"]
        snap["client_metrics"] = cli["client_metrics"]
        snap["links"] = nz.dedupe_links(dev["links"] + cli["links"])
        snap["wlans"] = nz.wlans(await self._optional("rest/wlanconf", warnings), self.site, now)
        snap["networks"] = nz.networks(await self._optional("rest/networkconf", warnings),
                                       self.site, now)
        gw = nz.gateway_mac(raw_devices)
        snap["routes"] = (nz.routes(await self._optional("stat/routing", warnings), gw,
                                    self.site, now) if gw else [])
        if not gw:
            warnings.append("no UniFi gateway among the devices; routing table not collected")
        self._cycle += 1
        return snap

    async def _fetch_integration(self, snap: dict[str, Any]) -> dict[str, Any]:
        c, now = self.client, snap["now"]
        site_id = await c.integration_site_id()
        if not site_id:
            raise UnifiError("integration API returned no sites")
        raw_devices = await c.integration_all(f"sites/{site_id}/devices")
        raw_clients = await c.integration_all(f"sites/{site_id}/clients")
        cli = nz.integration_clients(raw_clients, raw_devices, self.site, now)
        snap.update({
            "mode": "integration",
            "devices": nz.integration_devices(raw_devices, self.site, now),
            "device_metrics": [], "radio_metrics": [], "port_metrics": [],
            "clients": cli["clients"], "client_metrics": [], "links": cli["links"],
            "wlans": [], "networks": [], "routes": [],
        })
        return snap

    async def store(self, pool: asyncpg.Pool, snap: dict[str, Any]) -> dict[str, int]:
        async with pool.acquire() as conn, conn.transaction():
            await db.upsert_rows(conn, "devices", snap["devices"], ("src", "mac"))
            await db.insert_rows(conn, "device_metrics", snap["device_metrics"])
            await db.insert_rows(conn, "radio_metrics", snap["radio_metrics"])
            await db.insert_rows(conn, "port_metrics", snap["port_metrics"])

            await conn.execute(
                "UPDATE clients SET connected = false WHERE src = $1 AND site = $2 AND connected",
                self.name, self.site)
            await db.upsert_rows(conn, "clients", snap["clients"], ("src", "mac"),
                                 overrides=_CLIENT_OVERRIDES)
            await db.insert_rows(conn, "client_metrics", snap["client_metrics"])

            await db.upsert_rows(conn, "wlans", snap["wlans"], ("src", "wlan_id"))
            await db.upsert_rows(conn, "networks", snap["networks"], ("src", "network_id"))
            await db.upsert_rows(conn, "routes", snap["routes"],
                                 ("src", "device_mac", "prefix", "next_hop", "interface"))
            await db.upsert_rows(conn, "topology_links", snap["links"],
                                 ("src", "kind", "a_id", "a_port", "b_id", "b_port"))
            # forget adjacencies and routes that have not been seen for a day
            await conn.execute(
                "DELETE FROM topology_links WHERE src = $1 AND last_seen < now() - interval '1 day'",
                self.name)
            await conn.execute(
                "DELETE FROM routes WHERE src = $1 AND last_seen < now() - interval '1 day'",
                self.name)
        return {k: len(snap[k]) for k in ("devices", "clients", "client_metrics", "radio_metrics",
                                          "port_metrics", "wlans", "networks", "routes", "links")}

    async def run_once(self, pool: asyncpg.Pool) -> tuple[dict[str, int], list[str]]:
        snap = await self.fetch()
        counts = await self.store(pool, snap)
        return counts, snap["warnings"]
