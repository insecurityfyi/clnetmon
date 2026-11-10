"""Pure functions turning raw UniFi controller JSON into rows for the NMS schema.

No I/O here so everything is unit-testable. Controller fields come and go between
firmware versions, so every lookup is tolerant of missing/odd values; the complete
original object is kept in the "raw" column of the current-state tables.
"""
from __future__ import annotations

import ipaddress
from datetime import datetime, timezone
from typing import Any

SRC = "unifi"

DEVICE_STATES = {
    0: "offline", 1: "online", 2: "pending", 4: "upgrading", 5: "provisioning",
    6: "heartbeat-missed", 7: "adopting", 9: "adoption-failed", 10: "isolated",
    11: "rf-scanning",
}
BANDS = {"ng": "2.4", "na": "5", "6e": "6"}
ROUTE_PROTOCOLS = {"C": "connected", "S": "static", "K": "kernel", "O": "ospf",
                   "B": "bgp", "R": "rip", "I": "isis"}
GATEWAY_TYPES = ("ugw", "udm", "uxg")


# ------------------------------------------------------------------ coercion helpers
def _mac(v: Any) -> str | None:
    return v.strip().lower() if isinstance(v, str) and v.strip() else None


def _i(v: Any) -> int | None:
    try:
        return None if v is None or isinstance(v, bool) else int(float(v))
    except (TypeError, ValueError):
        return None


def _f(v: Any) -> float | None:
    try:
        return None if v is None or isinstance(v, bool) else float(v)
    except (TypeError, ValueError):
        return None


def _b(v: Any) -> bool | None:
    return v if isinstance(v, bool) else None


def _s(v: Any) -> str | None:
    if v is None or isinstance(v, (dict, list)):
        return None
    s = str(v).strip()
    return s or None


def _ts(v: Any) -> datetime | None:
    n = _f(v)
    if not n or n <= 0:
        return None
    if n > 1e11:  # milliseconds
        n /= 1000
    return datetime.fromtimestamp(n, tz=timezone.utc)


def _pct(used: Any, total: Any) -> float | None:
    u, t = _f(used), _f(total)
    return round(100 * u / t, 1) if u is not None and t else None


def strip_secrets(obj: Any) -> Any:
    """Drop controller secrets (x_passphrase, x_iapp_key, ... ) before storing raw JSON."""
    if isinstance(obj, dict):
        return {k: strip_secrets(v) for k, v in obj.items()
                if not (isinstance(k, str) and (k.startswith("x_") or "passphrase" in k
                                                or "password" in k or "secret" in k))}
    if isinstance(obj, list):
        return [strip_secrets(v) for v in obj]
    return obj


# ------------------------------------------------------------------ devices
def devices(raw: list[dict[str, Any]], site: str, now: datetime) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {
        "devices": [], "device_metrics": [], "radio_metrics": [], "port_metrics": [], "links": [],
    }
    for d in raw:
        mac = _mac(d.get("mac"))
        if not mac:
            continue
        uplink = d.get("uplink") if isinstance(d.get("uplink"), dict) else {}
        uplink_mac = _mac(uplink.get("uplink_mac"))
        sys_stats = d.get("sys_stats") or {}
        system_stats = d.get("system-stats") or {}

        out["devices"].append({
            "src": SRC, "site": site, "mac": mac,
            "name": _s(d.get("name")) or _s(d.get("hostname")),
            "type": _s(d.get("type")),
            "model": _s(d.get("model")),
            "serial": _s(d.get("serial")),
            "ip": _s(d.get("ip")),
            "version": _s(d.get("version")),
            "state": DEVICE_STATES.get(_i(d.get("state")), _s(d.get("state"))),
            "adopted": _b(d.get("adopted")),
            "upgradable": _b(d.get("upgradable")),
            "uptime_s": _i(d.get("uptime")),
            "uplink_mac": uplink_mac,
            "last_seen": _ts(d.get("last_seen")),
            "updated_at": now,
            "raw": strip_secrets(d),
        })
        out["device_metrics"].append({
            "time": now, "src": SRC, "mac": mac,
            "cpu_pct": _f(system_stats.get("cpu")),
            "mem_pct": _f(system_stats.get("mem"))
                       or _pct(sys_stats.get("mem_used"), sys_stats.get("mem_total")),
            "load_1": _f(sys_stats.get("loadavg_1")),
            "temperature": _f(d.get("general_temperature")),
            "num_clients": _i(d.get("num_sta")),
            "num_guests": _i(d.get("guest-num_sta")),
            "satisfaction": _i(d.get("satisfaction")),
            "tx_bytes": _i(d.get("tx_bytes")),
            "rx_bytes": _i(d.get("rx_bytes")),
        })

        # radios: configuration (radio_table) merged with live stats (radio_table_stats)
        cfg = {r.get("name"): r for r in d.get("radio_table") or [] if isinstance(r, dict)}
        stats = {r.get("name"): r for r in d.get("radio_table_stats") or [] if isinstance(r, dict)}
        for name in sorted(set(cfg) | set(stats), key=str):
            if not name:
                continue
            c, s = cfg.get(name, {}), stats.get(name, {})
            radio_code = _s(s.get("radio")) or _s(c.get("radio"))
            out["radio_metrics"].append({
                "time": now, "src": SRC, "device_mac": mac, "radio": str(name),
                "band": BANDS.get(radio_code, radio_code),
                "channel": _i(s.get("channel")) or _i(c.get("channel")),
                "width_mhz": _i(c.get("ht")) or _i(c.get("channel_width")),
                "tx_power_dbm": _i(s.get("tx_power")) or _i(c.get("tx_power")),
                "util_total": _i(s.get("cu_total")),
                "util_self_rx": _i(s.get("cu_self_rx")),
                "util_self_tx": _i(s.get("cu_self_tx")),
                "num_clients": _i(s.get("num_sta")),
                "satisfaction": _i(s.get("satisfaction")),
                "tx_retries": _i(s.get("tx_retries")),
                "tx_packets": _i(s.get("tx_packets")),
            })

        for p in d.get("port_table") or []:
            idx = _i(p.get("port_idx")) if isinstance(p, dict) else None
            if idx is None:
                continue
            out["port_metrics"].append({
                "time": now, "src": SRC, "device_mac": mac, "port_idx": idx,
                "name": _s(p.get("name")),
                "up": _b(p.get("up")),
                "speed_mbps": _i(p.get("speed")),
                "full_duplex": _b(p.get("full_duplex")),
                "poe_w": _f(p.get("poe_power")),
                "tx_bytes": _i(p.get("tx_bytes")),
                "rx_bytes": _i(p.get("rx_bytes")),
                "tx_errors": _i(p.get("tx_errors")),
                "rx_errors": _i(p.get("rx_errors")),
                "tx_dropped": _i(p.get("tx_dropped")),
                "rx_dropped": _i(p.get("rx_dropped")),
            })

        # topology: uplink reported by the controller
        if uplink_mac and uplink_mac != mac:
            wireless = _s(uplink.get("type")) == "wireless"
            out["links"].append(_link(
                site, "uplink", mac, _s(uplink.get("port_idx")), uplink_mac,
                _s(uplink.get("uplink_remote_port")),
                "wireless" if wireless else "wired", _i(uplink.get("speed")), now,
                {"name": _s(uplink.get("uplink_device_name"))},
            ))
        # topology: LLDP neighbours heard by this device (includes non-UniFi gear)
        for n in d.get("lldp_table") or []:
            if not isinstance(n, dict):
                continue
            chassis = _mac(n.get("chassis_id"))
            if not chassis or chassis == mac:
                continue
            out["links"].append(_link(
                site, "lldp", mac,
                _s(n.get("local_port_idx")) or _s(n.get("local_port_name")),
                chassis, _s(n.get("port_id")), "wired", None, now,
                {"local_port_name": _s(n.get("local_port_name")),
                 "chassis_descr": _s(n.get("chassis_descr")),
                 "port_descr": _s(n.get("port_descr"))},
            ))
    return out


def _link(site: str, kind: str, a: str, a_port: str | None, b: str, b_port: str | None,
          medium: str | None, speed: int | None, now: datetime, detail: dict) -> dict:
    return {
        "src": SRC, "site": site, "kind": kind,
        "a_id": a, "a_port": a_port or "", "b_id": b, "b_port": b_port or "",
        "medium": medium, "speed_mbps": speed,
        "detail": {k: v for k, v in detail.items() if v is not None},
        "last_seen": now,
    }


def dedupe_links(links: list[dict]) -> list[dict]:
    seen: dict[tuple, dict] = {}
    for l in links:
        seen[(l["src"], l["kind"], l["a_id"], l["a_port"], l["b_id"], l["b_port"])] = l
    return list(seen.values())


# ------------------------------------------------------------------ clients
def _client_base(site: str, mac: str, now: datetime) -> dict[str, Any]:
    return {
        "src": SRC, "site": site, "mac": mac,
        "hostname": None, "name": None, "display_name": None, "identity": None,
        "oui": None, "os_name": None, "dev_vendor": None, "dev_family": None,
        "is_wired": None, "is_guest": None, "blocked": None, "fixed_ip": None,
        "note": None, "first_seen": None, "last_seen": None, "connected": False,
        "ip": None, "ap_mac": None, "sw_mac": None, "sw_port": None, "essid": None,
        "bssid": None, "network": None, "vlan": None, "updated_at": now, "raw": None,
    }


def _client_identity(row: dict[str, Any], c: dict[str, Any]) -> None:
    row["hostname"] = _s(c.get("hostname")) or row["hostname"]
    row["name"] = _s(c.get("name")) or row["name"]
    row["display_name"] = (row["name"] or row["hostname"] or _s(c.get("display_name"))
                           or row["display_name"])
    row["identity"] = _s(c.get("1x_identity")) or row["identity"]
    row["oui"] = _s(c.get("oui")) or row["oui"]
    row["os_name"] = _s(c.get("os_name")) or row["os_name"]
    row["dev_vendor"] = _s(c.get("dev_vendor")) or row["dev_vendor"]
    row["dev_family"] = _s(c.get("dev_family")) or row["dev_family"]
    for k in ("is_wired", "is_guest", "blocked"):
        if _b(c.get(k)) is not None:
            row[k] = c[k]
    if c.get("use_fixedip") and _s(c.get("fixed_ip")):
        row["fixed_ip"] = _s(c.get("fixed_ip"))
    row["note"] = _s(c.get("note")) or row["note"]
    row["first_seen"] = _ts(c.get("first_seen")) or row["first_seen"]
    row["last_seen"] = _ts(c.get("last_seen")) or row["last_seen"]


def clients(active: list[dict[str, Any]], known: list[dict[str, Any]], site: str,
            now: datetime) -> dict[str, list[dict]]:
    """active = stat/sta (connected now); known = rest/user (every client on record)."""
    rows: dict[str, dict] = {}
    metrics: list[dict] = []
    links: list[dict] = []

    for c in known:
        mac = _mac(c.get("mac"))
        if not mac:
            continue
        row = rows.setdefault(mac, _client_base(site, mac, now))
        _client_identity(row, c)
        row["raw"] = strip_secrets(c)

    for c in active:
        mac = _mac(c.get("mac"))
        if not mac:
            continue
        row = rows.setdefault(mac, _client_base(site, mac, now))
        _client_identity(row, c)
        wired = bool(c.get("is_wired"))
        ap_mac = None if wired else _mac(c.get("ap_mac"))
        sw_mac = _mac(c.get("sw_mac")) if wired else None
        sw_port = _i(c.get("sw_port")) if wired else None
        row.update({
            "connected": True, "is_wired": wired, "last_seen": row["last_seen"] or now,
            "ip": _s(c.get("ip")), "ap_mac": ap_mac, "sw_mac": sw_mac, "sw_port": sw_port,
            "essid": _s(c.get("essid")), "bssid": _mac(c.get("bssid")),
            "network": _s(c.get("network")), "vlan": _i(c.get("vlan")),
            "raw": strip_secrets(c),
        })
        radio_code = _s(c.get("radio"))
        metrics.append({
            "time": now, "src": SRC, "mac": mac, "ip": row["ip"], "is_wired": wired,
            "ap_mac": ap_mac, "sw_mac": sw_mac, "sw_port": sw_port, "essid": row["essid"],
            "band": BANDS.get(radio_code, radio_code),
            "radio_proto": _s(c.get("radio_proto")),
            "channel": _i(c.get("channel")),
            "signal_dbm": _i(c.get("signal")),
            "noise_dbm": _i(c.get("noise")),
            "rssi": _i(c.get("rssi")),
            "tx_rate_kbps": _i(c.get("tx_rate")),
            "rx_rate_kbps": _i(c.get("rx_rate")),
            "tx_bytes": _i(c.get("wired-tx_bytes") if wired else c.get("tx_bytes")),
            "rx_bytes": _i(c.get("wired-rx_bytes") if wired else c.get("rx_bytes")),
            "tx_bps": _bps(c.get("wired-tx_bytes-r") if wired else c.get("tx_bytes-r")),
            "rx_bps": _bps(c.get("wired-rx_bytes-r") if wired else c.get("rx_bytes-r")),
            "tx_retries": _i(c.get("tx_retries")),
            "satisfaction": _i(c.get("satisfaction")),
            "uptime_s": _i(c.get("uptime")),
            "roam_count": _i(c.get("roam_count")),
        })
        parent = sw_mac if wired else ap_mac
        if parent and parent != mac:
            links.append(_link(
                site, "client", mac, "", parent, str(sw_port) if sw_port is not None else "",
                "wired" if wired else "wireless", None, now,
                {"essid": row["essid"]} if not wired else {},
            ))
    return {"clients": list(rows.values()), "client_metrics": metrics, "links": links}


def _bps(bytes_per_s: Any) -> float | None:
    v = _f(bytes_per_s)
    return None if v is None else v * 8


# ------------------------------------------------------------------ wlans / networks / routes
def wlans(raw: list[dict[str, Any]], site: str, now: datetime) -> list[dict]:
    out = []
    for w in raw:
        wid = _s(w.get("_id"))
        if not wid:
            continue
        bands = w.get("wlan_bands")
        out.append({
            "src": SRC, "site": site, "wlan_id": wid,
            "name": _s(w.get("name")),
            "enabled": _b(w.get("enabled")),
            "security": _s(w.get("security")),
            "wpa_mode": _s(w.get("wpa_mode")),
            "wpa3": _b(w.get("wpa3_support")),
            "is_guest": _b(w.get("is_guest")),
            "hidden": _b(w.get("hide_ssid")),
            "bands": ",".join(map(str, bands)) if isinstance(bands, list) else _s(w.get("wlan_band")),
            "network_id": _s(w.get("networkconf_id")),
            "updated_at": now,
            "raw": strip_secrets(w),
        })
    return out


def networks(raw: list[dict[str, Any]], site: str, now: datetime) -> list[dict]:
    out = []
    for n in raw:
        nid = _s(n.get("_id"))
        if not nid:
            continue
        subnet = gateway_ip = None
        cidr = _s(n.get("ip_subnet"))  # controller stores "gateway-ip/prefixlen"
        if cidr:
            try:
                iface = ipaddress.ip_interface(cidr)
                subnet, gateway_ip = str(iface.network), str(iface.ip)
            except ValueError:
                pass
        out.append({
            "src": SRC, "site": site, "network_id": nid,
            "name": _s(n.get("name")),
            "purpose": _s(n.get("purpose")),
            "vlan": _i(n.get("vlan")),
            "subnet": subnet, "gateway_ip": gateway_ip,
            "dhcp": _b(n.get("dhcpd_enabled")),
            "domain": _s(n.get("domain_name")),
            "updated_at": now,
            "raw": strip_secrets(n),
        })
    return out


def gateway_mac(raw_devices: list[dict[str, Any]]) -> str | None:
    """The device whose routing table stat/routing describes."""
    for d in raw_devices:
        if _s(d.get("type")) in GATEWAY_TYPES and _mac(d.get("mac")):
            return _mac(d.get("mac"))
    return None


def routes(raw: list[dict[str, Any]], device_mac: str, site: str, now: datetime) -> list[dict]:
    out: dict[tuple, dict] = {}
    for r in raw:
        prefix = _s(r.get("pfx"))
        if not prefix:
            continue
        hops = r.get("nh") if isinstance(r.get("nh"), list) and r.get("nh") else [{}]
        for h in hops:
            if not isinstance(h, dict):
                continue
            flags = _s(h.get("t")) or ""
            metric = _s(h.get("metric"))
            row = {
                "src": SRC, "site": site, "device_mac": device_mac, "prefix": prefix,
                "next_hop": _s(h.get("via")) or "",
                "interface": _s(h.get("intf_name")) or "",
                "protocol": ROUTE_PROTOCOLS.get(flags[:1], flags[:1].lower() or None),
                "metric": _i(metric.split("/")[-1]) if metric else None,
                "selected": (">" in flags) if flags else None,
                "last_seen": now,
            }
            out[(prefix, row["next_hop"], row["interface"])] = row
    return list(out.values())


# ------------------------------------------------------------------ integration-API fallback
def integration_devices(raw: list[dict[str, Any]], site: str, now: datetime) -> list[dict]:
    out = []
    for d in raw:
        mac = _mac(d.get("macAddress"))
        if not mac:
            continue
        features = d.get("features") or []
        kind = ("uap" if "accessPoint" in features else
                "usw" if "switching" in features else None)
        out.append({
            "src": SRC, "site": site, "mac": mac, "name": _s(d.get("name")), "type": kind,
            "model": _s(d.get("model")), "serial": None, "ip": _s(d.get("ipAddress")),
            "version": _s(d.get("firmwareVersion")),
            "state": (_s(d.get("state")) or "").lower() or None,
            "adopted": True, "upgradable": _b(d.get("firmwareUpdatable")), "uptime_s": None,
            "uplink_mac": None, "last_seen": None, "updated_at": now, "raw": d,
        })
    return out


def integration_clients(raw: list[dict[str, Any]], raw_devices: list[dict[str, Any]],
                        site: str, now: datetime) -> dict[str, list[dict]]:
    dev_mac = {d.get("id"): _mac(d.get("macAddress")) for d in raw_devices}
    rows, links = [], []
    for c in raw:
        mac = _mac(c.get("macAddress"))
        if not mac:
            continue
        wired = _s(c.get("type")) == "WIRED"
        parent = dev_mac.get(c.get("uplinkDeviceId"))
        row = _client_base(site, mac, now)
        row.update({
            "name": _s(c.get("name")), "display_name": _s(c.get("name")),
            "is_wired": wired, "connected": True, "ip": _s(c.get("ipAddress")),
            "ap_mac": None if wired else parent, "sw_mac": parent if wired else None,
            "last_seen": now, "raw": c,
        })
        rows.append(row)
        if parent and parent != mac:
            links.append(_link(site, "client", mac, "", parent, "",
                               "wired" if wired else "wireless", None, now, {}))
    return {"clients": rows, "client_metrics": [], "links": links}
