from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..config import get_settings

router = APIRouter(prefix="/api/v1")


def require_token(request: Request) -> None:
    token = get_settings().api_token
    if not token:
        return
    sent = request.headers.get("authorization", "")
    if not hmac.compare_digest(sent, f"Bearer {token}"):
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


router.dependencies.append(Depends(require_token))


async def _rows(request: Request, sql: str, *args: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in await request.app.state.pool.fetch(sql, *args)]


def _q(alias: str, cols: str) -> str:
    return ", ".join(f"{alias}.{c}" for c in cols.split())


_DEVICE_COLS = _q("d", "src site mac name type model serial ip version state adopted "
                       "upgradable uptime_s uplink_mac last_seen updated_at")
_CLIENT_COLS = _q("c", "src site mac hostname name display_name identity oui os_name "
                       "dev_vendor dev_family is_wired is_guest blocked fixed_ip note first_seen "
                       "last_seen connected ip ap_mac sw_mac sw_port essid bssid network vlan "
                       "updated_at")


@router.get("/collectors")
async def collectors(request: Request):
    """Most recent run of each collector."""
    return await _rows(request, """
        SELECT DISTINCT ON (src) src, started_at, finished_at, ok, counts, warnings, error
        FROM collector_runs ORDER BY src, started_at DESC""")


@router.get("/devices")
async def devices(request: Request):
    return await _rows(request, f"""
        SELECT {_DEVICE_COLS}, m.cpu_pct, m.mem_pct, m.temperature, m.num_clients, m.satisfaction
        FROM devices d
        LEFT JOIN LATERAL (SELECT * FROM device_metrics m WHERE m.mac = d.mac
                           ORDER BY time DESC LIMIT 1) m ON true
        ORDER BY d.name""")


@router.get("/devices/{mac}")
async def device(request: Request, mac: str):
    mac = mac.lower()
    rows = await _rows(request, "SELECT * FROM devices WHERE mac = $1", mac)
    if not rows:
        raise HTTPException(404, "device not found")
    out = rows[0]
    out["radios"] = await _rows(request, """
        SELECT DISTINCT ON (radio) * FROM radio_metrics WHERE device_mac = $1
        ORDER BY radio, time DESC""", mac)
    out["ports"] = await _rows(request, """
        SELECT DISTINCT ON (port_idx) * FROM port_metrics WHERE device_mac = $1
        ORDER BY port_idx, time DESC""", mac)
    return out


@router.get("/clients")
async def clients(request: Request,
                  connected: bool | None = Query(None, description="filter on connected now"),
                  wired: bool | None = None,
                  search: str | None = Query(None, description="matches name, hostname, MAC, IP")):
    return await _rows(request, f"""
        SELECT {_CLIENT_COLS}, m.band, m.channel, m.signal_dbm, m.rx_rate_kbps, m.tx_rate_kbps,
               m.satisfaction, m.uptime_s
        FROM clients c
        LEFT JOIN LATERAL (SELECT * FROM client_metrics m
                           WHERE m.mac = c.mac AND m.time > now() - interval '10 minutes'
                           ORDER BY time DESC LIMIT 1) m ON c.connected
        WHERE ($1::boolean IS NULL OR c.connected = $1)
          AND ($2::boolean IS NULL OR c.is_wired = $2)
          AND ($3::text IS NULL OR concat_ws(' ', c.display_name, c.hostname, c.mac, c.ip,
                                             c.identity) ILIKE '%' || $3 || '%')
        ORDER BY c.connected DESC, c.display_name NULLS LAST""", connected, wired, search)


@router.get("/clients/{mac}")
async def client(request: Request, mac: str, hours: int = Query(24, ge=1, le=24 * 90)):
    mac = mac.lower()
    rows = await _rows(request, "SELECT * FROM clients WHERE mac = $1", mac)
    if not rows:
        raise HTTPException(404, "client not found")
    out = rows[0]
    out["history"] = await _rows(request, """
        SELECT * FROM client_metrics
        WHERE mac = $1 AND time > now() - make_interval(hours => $2) ORDER BY time""", mac, hours)
    return out


@router.get("/wireless/radios")
async def radios(request: Request):
    """Latest sample for every AP radio."""
    return await _rows(request, """
        SELECT DISTINCT ON (r.device_mac, r.radio) d.name AS device_name, r.*
        FROM radio_metrics r JOIN devices d ON d.mac = r.device_mac
        WHERE r.time > now() - interval '15 minutes'
        ORDER BY r.device_mac, r.radio, r.time DESC""")


@router.get("/wireless/wlans")
async def wlans(request: Request):
    return await _rows(request, """
        SELECT w.src, w.site, w.wlan_id, w.name, w.enabled, w.security, w.wpa_mode, w.wpa3,
               w.is_guest, w.hidden, w.bands, n.name AS network, n.vlan,
               (SELECT count(*) FROM clients c WHERE c.connected AND c.essid = w.name) AS clients
        FROM wlans w LEFT JOIN networks n ON n.network_id = w.network_id ORDER BY w.name""")


@router.get("/networks")
async def networks(request: Request):
    return await _rows(request, """
        SELECT src, site, network_id, name, purpose, vlan, subnet, gateway_ip, dhcp, domain
        FROM networks ORDER BY vlan NULLS FIRST, name""")


@router.get("/routes")
async def routes(request: Request):
    return await _rows(request, """
        SELECT r.*, d.name AS device_name FROM routes r
        LEFT JOIN devices d ON d.mac = r.device_mac
        WHERE r.last_seen > now() - interval '15 minutes' ORDER BY r.prefix""")


@router.get("/topology")
async def topology(request: Request,
                   include_clients: bool = Query(False, description="add end-user clients"),
                   include_routes: bool = Query(True, description="add routed prefixes (L3)")):
    """Current network map as a graph: built from uplinks, LLDP neighbours, client
    attachment and the gateway routing table at the last collection."""
    skip = [k for k, on in (("client", include_clients), ("network", include_routes)) if not on]
    skip_edges = [k for k, on in (("client", include_clients), ("route", include_routes)) if not on]
    nodes = await _rows(request,
                        "SELECT * FROM topology_nodes WHERE kind <> ALL($1::text[])", skip)
    edges = await _rows(request,
                        "SELECT * FROM topology_edges WHERE kind <> ALL($1::text[])", skip_edges)
    ids = {n["id"] for n in nodes}
    edges = [e for e in edges if e["source"] in ids and e["target"] in ids]
    return {"nodes": nodes, "edges": edges}
