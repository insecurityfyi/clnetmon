"""What to ask a device for, and how to turn the answers into NMS rows.

Everything here works against a "session" with get(*oids) and walk(base) so tests can
supply canned data. Only standard MIBs are used (MIB-II, IF-MIB, LLDP-MIB, BRIDGE-MIB,
Q-BRIDGE-MIB, IP-FORWARD-MIB, HOST-RESOURCES-MIB, Printer-MIB), so any vendor's
switch, firewall, router, AP or printer yields the same shape of data.
"""
from __future__ import annotations

import ipaddress
import re
from datetime import datetime
from typing import Any

SRC = "snmp"

SYS_DESCR, SYS_OBJECT_ID, SYS_UPTIME = "1.3.6.1.2.1.1.1.0", "1.3.6.1.2.1.1.2.0", "1.3.6.1.2.1.1.3.0"
SYS_CONTACT, SYS_NAME, SYS_LOCATION = "1.3.6.1.2.1.1.4.0", "1.3.6.1.2.1.1.5.0", "1.3.6.1.2.1.1.6.0"
IP_FORWARDING = "1.3.6.1.2.1.4.1.0"
BRIDGE_ADDRESS = "1.3.6.1.2.1.17.1.1.0"
LLDP_LOC_CHASSIS_SUBTYPE, LLDP_LOC_CHASSIS_ID = "1.0.8802.1.1.2.1.3.1.0", "1.0.8802.1.1.2.1.3.2.0"

IF_TABLE, IFX_TABLE = "1.3.6.1.2.1.2.2.1", "1.3.6.1.2.1.31.1.1.1"
LLDP_LOC_PORT, LLDP_REM = "1.0.8802.1.1.2.1.3.7.1", "1.0.8802.1.1.2.1.4.1.1"
INET_CIDR_ROUTE, IP_CIDR_ROUTE, IP_ROUTE = ("1.3.6.1.2.1.4.24.7.1", "1.3.6.1.2.1.4.24.4.1",
                                            "1.3.6.1.2.1.4.21.1")
ARP_OLD, ARP_NEW = "1.3.6.1.2.1.4.22.1.2", "1.3.6.1.2.1.4.35.1.4"
FDB_Q, FDB_D, BASE_PORT_IFINDEX = ("1.3.6.1.2.1.17.7.1.2.2.1.2", "1.3.6.1.2.1.17.4.3.1.2",
                                   "1.3.6.1.2.1.17.1.4.1.2")
HR_PROCESSOR_LOAD, HR_STORAGE = "1.3.6.1.2.1.25.3.3.1.2", "1.3.6.1.2.1.25.2.3.1"
HR_STORAGE_RAM = "1.3.6.1.2.1.25.2.1.2"
HR_DEVICE_DESCR, HR_PRINTER_STATUS = "1.3.6.1.2.1.25.3.2.1.3", "1.3.6.1.2.1.25.3.5.1.1"
PRT_SUPPLIES, PRT_PAGE_COUNT = "1.3.6.1.2.1.43.11.1.1", "1.3.6.1.2.1.43.10.2.1.4"
PRT_CONSOLE_TEXT = "1.3.6.1.2.1.43.16.5.1.2"

ROUTE_PROTO = {2: "connected", 3: "static", 4: "icmp", 8: "rip", 9: "isis", 13: "ospf",
               14: "bgp", 16: "eigrp"}
PRINTER_STATUS = {1: "other", 2: "unknown", 3: "idle", 4: "printing", 5: "warming up"}
FIREWALL_HINT = re.compile(
    r"firewall|fortigate|fortios|pfsense|opnsense|sonicwall|palo alto|pan-os|adaptive security|"
    r"sophos|watchguard|checkpoint|check point|juniper srx|meraki mx|untangle|firepower", re.I)

MAX_IFACES, MAX_ROUTES, MAX_FDB, MAX_ARP = 1024, 2000, 20000, 10000


# ------------------------------------------------------------------ small helpers
def mac(b: Any) -> str | None:
    if isinstance(b, bytes) and len(b) == 6 and any(b) and b != b"\xff" * 6:
        return ":".join(f"{x:02x}" for x in b)
    if isinstance(b, bytes) and len(b) == 17:  # some agents send the MAC as text
        t = b.decode("ascii", "ignore").lower().replace("-", ":")
        if re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", t):
            return t
    return None


def text(b: Any) -> str | None:
    if b is None:
        return None
    if isinstance(b, bytes):
        b = b.decode("utf-8", "replace")
    s = "".join(ch for ch in str(b) if ch.isprintable()).strip()
    return s or None


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


async def columns(session, base: str, cols: tuple[int, ...], limit: int = 20000
                  ) -> dict[tuple[int, ...], dict[int, Any]]:
    """Walk selected columns of a table -> {row index: {column: value}}."""
    rows: dict[tuple[int, ...], dict[int, Any]] = {}
    for col in cols:
        for idx, val in await session.walk(f"{base}.{col}", limit=limit):
            rows.setdefault(idx, {})[col] = val
    return rows


# ------------------------------------------------------------------ one device
async def poll_device(session, host: str, now: datetime, known_macs: frozenset[str] = frozenset()
                      ) -> dict[str, Any]:
    """Read everything from one device. Raises SnmpError if it does not answer at all.

    known_macs: MACs already known from other sources (UniFi devices and clients). If one
    of this device's own MACs is among them it becomes the device id, so the SNMP view and
    the UniFi view of the same box land on the same map node.
    """
    sysv = await session.get(SYS_DESCR, SYS_OBJECT_ID, SYS_UPTIME, SYS_CONTACT, SYS_NAME,
                             SYS_LOCATION)
    extra = {}
    for oid in (IP_FORWARDING, BRIDGE_ADDRESS, LLDP_LOC_CHASSIS_SUBTYPE, LLDP_LOC_CHASSIS_ID):
        try:
            extra.update(await session.get(oid))
        except Exception:
            pass  # optional scalars; some agents error instead of returning noSuchObject
    warnings: list[str] = []

    async def safe(coro, what: str, default):
        try:
            return await coro
        except Exception as e:
            warnings.append(f"{host}: {what}: {e}")
            return default

    # ---- interfaces
    ift = await safe(columns(session, IF_TABLE, (2, 3, 5, 6, 7, 8, 10, 13, 14, 16, 19, 20),
                             MAX_IFACES), "interfaces", {})
    ifx = await safe(columns(session, IFX_TABLE, (1, 6, 10, 15, 18), MAX_IFACES), "ifXTable", {})
    if_name: dict[int, str] = {}
    if_macs: list[tuple[int, str]] = []
    ports = []
    for idx in sorted(ift):
        i = idx[0]
        r, x = ift[idx], ifx.get(idx, {})
        name = text(x.get(1)) or text(r.get(2)) or str(i)
        if_name[i] = name
        m = mac(r.get(6))
        if m:
            if_macs.append((r.get(3) or 0, m))
        high = _int(x.get(15))
        speed = high if high else (_int(r.get(5)) or 0) // 1_000_000 or None
        ports.append({
            "time": now, "src": SRC, "device_mac": None, "port_idx": i,
            "name": name[:120], "up": r.get(8) == 1 if r.get(8) is not None else None,
            "speed_mbps": speed, "full_duplex": None, "poe_w": None,
            "tx_bytes": _int(x.get(10)) if x.get(10) is not None else _int(r.get(16)),
            "rx_bytes": _int(x.get(6)) if x.get(6) is not None else _int(r.get(10)),
            "tx_errors": _int(r.get(20)), "rx_errors": _int(r.get(14)),
            "tx_dropped": _int(r.get(19)), "rx_dropped": _int(r.get(13)),
            "alias": text(x.get(18)), "admin_up": r.get(7) == 1 if r.get(7) is not None else None,
        })

    # ---- identity
    chassis = mac(extra.get(LLDP_LOC_CHASSIS_ID)) if extra.get(LLDP_LOC_CHASSIS_SUBTYPE) in (4, None) else None
    bridge = mac(extra.get(BRIDGE_ADDRESS))
    own = [m for m in (chassis, bridge) if m] + [m for _, m in if_macs]
    ethernet = sorted(m for t, m in if_macs if t == 6) or sorted(m for _, m in if_macs)
    device_id = (next((m for m in own if m in known_macs), None) or chassis or bridge
                 or (ethernet[0] if ethernet else None) or f"ip:{host}")
    for p in ports:
        p["device_mac"] = device_id

    # ---- LLDP neighbours
    links = []
    loc = await safe(columns(session, LLDP_LOC_PORT, (3, 4)), "LLDP local ports", {})
    rem = await safe(columns(session, LLDP_REM, (4, 5, 6, 7, 8, 9, 10)), "LLDP neighbors", {})
    for idx, r in rem.items():
        if len(idx) < 3:
            continue
        port_num = idx[1]
        lp = loc.get((port_num,), {})
        local_port = if_name.get(port_num) or text(lp.get(4)) or text(lp.get(3)) or str(port_num)
        sys_name = text(r.get(9))
        far = mac(r.get(5)) if r.get(4) in (4, None) else None
        far = far or (f"lldp:{sys_name}" if sys_name else None) or (
            f"lldp:{text(r.get(5))}" if text(r.get(5)) else None)
        if not far or far == device_id:
            continue
        far_port = mac(r.get(7)) if r.get(6) == 3 else None
        far_port = text(r.get(8)) if r.get(6) == 3 and text(r.get(8)) else (far_port or text(r.get(7)))
        links.append({
            "src": SRC, "site": SRC, "kind": "lldp", "a_id": device_id, "a_port": local_port,
            "b_id": far, "b_port": far_port or "", "medium": "wired", "speed_mbps": None,
            "detail": {k: v for k, v in (("sys_name", sys_name),
                                         ("sys_descr", (text(r.get(10)) or "")[:200] or None),
                                         ("port_descr", text(r.get(8)))) if v},
            "last_seen": now,
        })

    # ---- routes
    routes = await safe(_routes(session, device_id, if_name, now), "routing table", [])
    # ---- ARP and bridge forwarding table
    arp = await safe(_arp(session, device_id, if_name, now), "ARP table", [])
    fdb = await safe(_fdb(session, device_id, if_name, now), "MAC table", [])

    # ---- CPU / memory (HOST-RESOURCES-MIB)
    cpu = mem = None
    loads = [v for _, v in await safe(session.walk(HR_PROCESSOR_LOAD), "CPU", []) if _int(v) is not None]
    if loads:
        cpu = round(sum(loads) / len(loads), 1)
    stor = await safe(columns(session, HR_STORAGE, (2, 5, 6)), "storage", {})
    ram = [r for r in stor.values() if r.get(2) == HR_STORAGE_RAM and r.get(5)]
    if ram:
        mem = round(100 * sum(r.get(6, 0) for r in ram) / sum(r[5] for r in ram), 1)

    # ---- printer
    printer = await safe(_printer(session, device_id, now), "printer", None)

    descr = text(sysv.get(SYS_DESCR)) or ""
    if printer:
        kind = "printer"
    elif extra.get(IP_FORWARDING) == 1:
        kind = "firewall" if FIREWALL_HINT.search(descr) else "router"
    elif fdb or bridge:
        kind = "switch"
    else:
        kind = "firewall" if FIREWALL_HINT.search(descr) else "host"

    uptime = _int(sysv.get(SYS_UPTIME))
    device = {
        "src": SRC, "site": SRC, "mac": device_id,
        "name": text(sysv.get(SYS_NAME)) or host, "type": kind,
        "model": (printer or {}).get("model") or (descr[:100] or None),
        "serial": None, "ip": host, "version": None, "state": "online", "adopted": None,
        "upgradable": None, "uptime_s": uptime // 100 if uptime is not None else None,
        "uplink_mac": None, "last_seen": now, "updated_at": now,
        "raw": {"sysDescr": descr or None, "sysObjectID": sysv.get(SYS_OBJECT_ID),
                "sysLocation": text(sysv.get(SYS_LOCATION)),
                "sysContact": text(sysv.get(SYS_CONTACT)),
                "macs": sorted(set(own)),
                "printer": ({k: printer[k] for k in ("status", "page_count", "display")}
                            if printer else None)},
    }
    metrics = {
        "time": now, "src": SRC, "mac": device_id, "cpu_pct": cpu, "mem_pct": mem, "load_1": None,
        "temperature": None, "num_clients": None, "num_guests": None, "satisfaction": None,
        "tx_bytes": None, "rx_bytes": None,
    }
    return {
        "device": device, "device_metrics": metrics, "ports": ports, "links": links,
        "routes": routes, "arp": arp, "fdb": fdb, "warnings": warnings,
        "printer_metrics": ([{"time": now, "src": SRC, "device_mac": device_id,
                              "status": printer["status"], "page_count": printer["page_count"]}]
                            if printer else []),
        "printer_supplies": printer["supplies"] if printer else [],
    }


# ------------------------------------------------------------------ tables
def _prefix(dest: tuple[int, ...], masklen: int) -> str | None:
    try:
        return str(ipaddress.ip_network((".".join(map(str, dest)), masklen), strict=False))
    except ValueError:
        return None


def _route(device_id, prefix, next_hop, ifindex, proto, metric, if_name, now) -> dict:
    return {
        "src": SRC, "site": SRC, "device_mac": device_id, "prefix": prefix,
        "next_hop": "" if next_hop in (None, "0.0.0.0") else next_hop,
        "interface": if_name.get(ifindex, str(ifindex) if ifindex else "") if ifindex else "",
        "protocol": ROUTE_PROTO.get(proto, "other" if proto else None),
        "metric": metric if isinstance(metric, int) and metric >= 0 else None,
        "selected": True, "last_seen": now,
    }


async def _routes(session, device_id: str, if_name: dict[int, str], now: datetime) -> list[dict]:
    out: dict[tuple, dict] = {}

    def add(r: dict) -> None:
        out[(r["prefix"], r["next_hop"], r["interface"])] = r

    # inetCidrRouteTable: index = destType.destLen.dest.pfxLen.policyLen.policy.nhType.nhLen.nh
    rows = await columns(session, INET_CIDR_ROUTE, (7, 9, 12), MAX_ROUTES)
    for idx, r in rows.items():
        if len(idx) < 8 or idx[0] != 1 or idx[1] != 4:
            continue  # IPv4 only for now
        dest, plen = idx[2:6], idx[6]
        rest = idx[7:]
        rest = rest[1 + rest[0]:] if rest else ()
        nh = ".".join(map(str, rest[2:6])) if len(rest) >= 6 and rest[0] == 1 else None
        p = _prefix(dest, plen)
        if p:
            add(_route(device_id, p, nh, r.get(7), r.get(9), r.get(12), if_name, now))
    if not out:  # ipCidrRouteTable: index = dest.mask.tos.nexthop
        rows = await columns(session, IP_CIDR_ROUTE, (5, 7, 11), MAX_ROUTES)
        for idx, r in rows.items():
            if len(idx) != 13:
                continue
            masklen = ipaddress.ip_network("0.0.0.0/" + ".".join(map(str, idx[4:8]))).prefixlen
            p = _prefix(idx[0:4], masklen)
            if p:
                add(_route(device_id, p, ".".join(map(str, idx[9:13])), r.get(5), r.get(7),
                           r.get(11), if_name, now))
    if not out:  # ipRouteTable (MIB-II, deprecated but still the only one on older gear)
        rows = await columns(session, IP_ROUTE, (2, 3, 7, 9, 11), MAX_ROUTES)
        for idx, r in rows.items():
            if len(idx) != 4 or not isinstance(r.get(11), str):
                continue
            try:
                masklen = ipaddress.ip_network("0.0.0.0/" + r[11]).prefixlen
            except ValueError:
                continue
            p = _prefix(idx, masklen)
            if p:
                add(_route(device_id, p, r.get(7), r.get(2), r.get(9), r.get(3), if_name, now))
    return list(out.values())


async def _arp(session, device_id: str, if_name: dict[int, str], now: datetime) -> list[dict]:
    out: dict[str, dict] = {}
    for idx, val in await session.walk(ARP_OLD, limit=MAX_ARP):
        m = mac(val)
        if m and len(idx) == 5:
            ip = ".".join(map(str, idx[1:]))
            out[ip] = {"src": SRC, "device_mac": device_id, "ip": ip, "mac": m,
                       "interface": if_name.get(idx[0], str(idx[0])), "last_seen": now}
    if not out:  # ipNetToPhysicalTable: index = ifIndex.addrType.addrLen.addr
        for idx, val in await session.walk(ARP_NEW, limit=MAX_ARP):
            m = mac(val)
            if m and len(idx) == 7 and idx[1] == 1 and idx[2] == 4:
                ip = ".".join(map(str, idx[3:]))
                out[ip] = {"src": SRC, "device_mac": device_id, "ip": ip, "mac": m,
                           "interface": if_name.get(idx[0], str(idx[0])), "last_seen": now}
    return list(out.values())


async def _fdb(session, device_id: str, if_name: dict[int, str], now: datetime) -> list[dict]:
    port_if = {idx[0]: v for idx, v in await session.walk(BASE_PORT_IFINDEX) if len(idx) == 1}
    out: dict[tuple, dict] = {}

    def add(vlan: int, mac_idx: tuple[int, ...], bridge_port: Any) -> None:
        if len(mac_idx) != 6 or not isinstance(bridge_port, int) or bridge_port <= 0:
            return
        m = ":".join(f"{x:02x}" for x in mac_idx)
        ifindex = port_if.get(bridge_port, bridge_port)
        out[(m, vlan)] = {"src": SRC, "device_mac": device_id, "mac": m, "vlan": vlan,
                          "port_idx": ifindex, "port_name": if_name.get(ifindex, str(ifindex)),
                          "last_seen": now}

    for idx, val in await session.walk(FDB_Q, limit=MAX_FDB):   # per-VLAN table
        if len(idx) == 7:
            add(idx[0], idx[1:], val)
    if not out:
        for idx, val in await session.walk(FDB_D, limit=MAX_FDB):
            add(0, idx, val)
    return list(out.values())


async def _printer(session, device_id: str, now: datetime) -> dict | None:
    sup = await columns(session, PRT_SUPPLIES, (6, 8, 9))
    pages = [v for _, v in await session.walk(PRT_PAGE_COUNT) if isinstance(v, int)]
    if not sup and not pages:
        return None
    status = [v for _, v in await session.walk(HR_PRINTER_STATUS)]
    models = [text(v) for _, v in await session.walk(HR_DEVICE_DESCR)]
    display = [t for t in (text(v) for _, v in await session.walk(PRT_CONSOLE_TEXT)) if t]
    supplies = []
    for idx in sorted(sup):
        r = sup[idx]
        level, cap = _int(r.get(9)), _int(r.get(8))
        supplies.append({
            "time": now, "src": SRC, "device_mac": device_id,
            "name": (text(r.get(6)) or f"supply {idx[-1]}")[:120],
            # negative levels are codes: -3 "some remaining", -2 unknown, -1 not limited
            "level": level, "max_capacity": cap,
            "level_pct": round(100 * level / cap, 1) if level is not None and cap and cap > 0
                         and level >= 0 else None,
        })
    return {"model": next((m for m in models if m), None),
            "status": PRINTER_STATUS.get(status[0]) if status else None,
            "page_count": pages[0] if pages else None,
            "display": " / ".join(display)[:200] or None, "supplies": supplies}
