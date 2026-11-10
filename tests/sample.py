"""A small made-up UniFi site, shaped like the controller's legacy API responses."""
import httpx

GW, SW, AP1, AP2 = "AA:BB:CC:00:00:01", "aa:bb:cc:00:00:02", "aa:bb:cc:00:00:03", "aa:bb:cc:00:00:04"
NAS = "00:11:32:aa:bb:cc"

DEVICES = [
    {"mac": GW, "name": "Gateway", "type": "udm", "model": "UDMPRO", "ip": "192.168.1.1",
     "version": "4.3.6", "state": 1, "adopted": True, "uptime": 864000, "last_seen": 1794330000,
     "system-stats": {"cpu": "12.5", "mem": "61.0"}, "sys_stats": {"loadavg_1": "0.42"},
     "num_sta": 4, "tx_bytes": 123456789012, "rx_bytes": 98765432101,
     "uplink": {"type": "wire", "speed": 1000, "ip": "203.0.113.10"},
     "port_table": [{"port_idx": 2, "name": "Port 2", "up": True, "speed": 1000,
                     "full_duplex": True, "tx_bytes": 5000, "rx_bytes": 6000}],
     "lldp_table": [{"chassis_id": SW, "local_port_idx": 2, "port_id": "Port 1"}],
     "x_authkey": "must-not-be-stored"},
    {"mac": SW, "name": "Core Switch", "type": "usw", "model": "US24P250", "ip": "192.168.1.2",
     "version": "7.1.26", "state": 1, "adopted": True, "uptime": 500000,
     "system-stats": {"cpu": "3.1", "mem": "40.2"}, "general_temperature": 52,
     "uplink": {"uplink_mac": GW.lower(), "uplink_remote_port": 2, "port_idx": 1,
                "type": "wire", "speed": 1000},
     "port_table": [
         {"port_idx": 1, "name": "Uplink", "up": True, "speed": 1000, "full_duplex": True,
          "tx_bytes": 111, "rx_bytes": 222, "rx_errors": 0},
         {"port_idx": 5, "name": "AP Lobby", "up": True, "speed": 1000, "poe_power": "7.42"},
         {"port_idx": 8, "name": "NAS", "up": True, "speed": 1000}],
     "lldp_table": [
         {"chassis_id": GW.lower(), "local_port_idx": 1, "port_id": "Port 2"},
         {"chassis_id": NAS, "local_port_idx": 8, "port_id": "eth0"}]},
    {"mac": AP1, "name": "AP Lobby", "type": "uap", "model": "U7PRO", "ip": "192.168.1.3",
     "version": "8.0.19", "state": 1, "adopted": True, "num_sta": 2, "satisfaction": 96,
     "uplink": {"uplink_mac": SW, "uplink_remote_port": 5, "type": "wire", "speed": 1000},
     "radio_table": [{"name": "wifi0", "radio": "ng", "channel": 6, "ht": 20},
                     {"name": "wifi1", "radio": "na", "channel": 44, "ht": 80}],
     "radio_table_stats": [
         {"name": "wifi0", "radio": "ng", "channel": 6, "cu_total": 34, "cu_self_rx": 3,
          "cu_self_tx": 2, "num_sta": 1, "satisfaction": 93, "tx_power": 17},
         {"name": "wifi1", "radio": "na", "channel": 44, "cu_total": 11, "cu_self_rx": 1,
          "cu_self_tx": 4, "num_sta": 1, "satisfaction": 98, "tx_power": 23}]},
    {"mac": AP2, "name": "AP Garage", "type": "uap", "model": "U6MESH", "ip": "192.168.1.4",
     "version": "6.6.77", "state": 1, "adopted": True, "num_sta": 1,
     "uplink": {"uplink_mac": AP1, "type": "wireless", "rssi": 38},
     "radio_table": [{"name": "wifi1", "radio": "na", "channel": 44, "ht": 80}],
     "radio_table_stats": [{"name": "wifi1", "radio": "na", "channel": 44, "cu_total": 15,
                            "num_sta": 1, "tx_power": 22}]},
]

ACTIVE = [
    {"mac": "DE:AD:BE:EF:00:01", "hostname": "adams-iphone", "name": "Adam iPhone",
     "ip": "192.168.1.101", "is_wired": False, "is_guest": False, "ap_mac": AP1,
     "essid": "Home", "bssid": "aa:bb:cc:00:00:13", "radio": "na", "radio_proto": "ax",
     "channel": 44, "signal": -52, "noise": -95, "rssi": 43, "tx_rate": 866000,
     "rx_rate": 780000, "tx_bytes": 1000, "rx_bytes": 2000, "tx_bytes-r": 1250.5,
     "rx_bytes-r": 250.0, "satisfaction": 98, "uptime": 3600, "network": "LAN",
     "first_seen": 1790000000, "last_seen": 1794330000, "oui": "Apple", "roam_count": 2},
    {"mac": "de:ad:be:ef:00:02", "hostname": "thermostat", "ip": "192.168.20.15",
     "is_wired": False, "ap_mac": AP1, "essid": "IoT", "radio": "ng", "channel": 6,
     "signal": -71, "noise": -92, "tx_rate": 72000, "rx_rate": 65000, "vlan": 20,
     "network": "IoT", "satisfaction": 81, "uptime": 90000},
    {"mac": "de:ad:be:ef:00:03", "hostname": "garage-cam", "ip": "192.168.20.22",
     "is_wired": False, "ap_mac": AP2, "essid": "IoT", "radio": "na", "channel": 44,
     "signal": -64, "network": "IoT", "vlan": 20, "uptime": 40000},
    {"mac": "de:ad:be:ef:00:04", "hostname": "workstation", "ip": "192.168.1.50",
     "is_wired": True, "sw_mac": SW, "sw_port": 3, "network": "LAN",
     "wired-tx_bytes": 9000, "wired-rx_bytes": 8000, "wired-tx_bytes-r": 100, "uptime": 7200,
     "1x_identity": "adam"},
]

KNOWN = [
    {"mac": "de:ad:be:ef:00:01", "name": "Adam iPhone", "hostname": "adams-iphone",
     "use_fixedip": True, "fixed_ip": "192.168.1.101", "first_seen": 1790000000},
    {"mac": "de:ad:be:ef:00:99", "hostname": "old-laptop", "is_wired": False,
     "first_seen": 1700000000, "last_seen": 1780000000},
]

WLANS = [
    {"_id": "w1", "name": "Home", "enabled": True, "security": "wpapsk", "wpa_mode": "wpa2",
     "wpa3_support": True, "is_guest": False, "hide_ssid": False, "wlan_bands": ["2g", "5g"],
     "networkconf_id": "n1", "x_passphrase": "super-secret"},
    {"_id": "w2", "name": "IoT", "enabled": True, "security": "wpapsk", "networkconf_id": "n2",
     "wlan_band": "2g", "x_passphrase": "another-secret"},
]

NETWORKS = [
    {"_id": "n1", "name": "LAN", "purpose": "corporate", "ip_subnet": "192.168.1.1/24",
     "dhcpd_enabled": True, "domain_name": "home.arpa"},
    {"_id": "n2", "name": "IoT", "purpose": "corporate", "vlan": 20,
     "ip_subnet": "192.168.20.1/24", "dhcpd_enabled": True},
    {"_id": "n3", "name": "WAN", "purpose": "wan"},
]

ROUTING = [
    {"pfx": "0.0.0.0/0", "nh": [{"intf_name": "eth8", "metric": "1/0", "t": "S>*",
                                  "via": "203.0.113.1"}]},
    {"pfx": "192.168.1.0/24", "nh": [{"intf_name": "br0", "t": "C>*"}]},
    {"pfx": "192.168.20.0/24", "nh": [{"intf_name": "br20", "t": "C>*"}]},
    {"pfx": "10.50.0.0/24", "nh": [{"intf_name": "br0", "metric": "1/0", "t": "S>*",
                                     "via": "192.168.1.250"}]},
]

LEGACY = {"stat/device": DEVICES, "stat/sta": ACTIVE, "rest/user": KNOWN,
          "rest/wlanconf": WLANS, "rest/networkconf": NETWORKS, "stat/routing": ROUTING}

API_KEY = "test-key"


def transport(legacy_ok: bool = True, key_ok_on_legacy: bool = True) -> httpx.MockTransport:
    """Fake console. legacy_ok=False makes the detailed endpoints 404; key_ok_on_legacy=False
    makes them demand a session login instead of the API key."""
    state = {"session": False, "requests": []}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        state["requests"].append((req.method, path))
        has_key = req.headers.get("x-api-key") == API_KEY
        if path == "/api/auth/login":
            state["session"] = True
            return httpx.Response(200, json={}, headers={"set-cookie": "TOKEN=abc; Path=/"})
        if path.startswith("/proxy/network/integration/v1/"):
            if not has_key:
                return httpx.Response(401)
            tail = path.split("/v1/", 1)[1]
            if tail == "info":
                return httpx.Response(200, json={"applicationVersion": "10.0.160"})
            if tail == "sites":
                data = [{"id": "site-uuid", "internalReference": "default", "name": "Default"}]
            elif tail == "sites/site-uuid/devices":
                data = [{"id": f"dev-{i}", "name": d["name"], "model": d["model"],
                         "macAddress": d["mac"], "ipAddress": d["ip"], "state": "ONLINE",
                         "features": ["accessPoint"] if d["type"] == "uap" else ["switching"]}
                        for i, d in enumerate(DEVICES)]
            elif tail == "sites/site-uuid/clients":
                data = [{"id": f"cl-{i}", "name": c.get("hostname"), "macAddress": c["mac"],
                         "ipAddress": c["ip"], "type": "WIRED" if c["is_wired"] else "WIRELESS",
                         "uplinkDeviceId": "dev-1" if c["is_wired"] else "dev-2"}
                        for i, c in enumerate(ACTIVE)]
            else:
                return httpx.Response(404)
            offset = int(req.url.params.get("offset", 0))
            limit = min(int(req.url.params.get("limit", 25)), 3)  # force pagination
            page = data[offset:offset + limit]
            return httpx.Response(200, json={"offset": offset, "limit": limit,
                                             "count": len(page), "totalCount": len(data),
                                             "data": page})
        prefix = "/proxy/network/api/s/default/"
        if path.startswith(prefix):
            if not legacy_ok:
                return httpx.Response(404)
            authed = (has_key and key_ok_on_legacy) or (state["session"] and "TOKEN=abc"
                                                        in req.headers.get("cookie", ""))
            if not authed:
                return httpx.Response(401, json={"meta": {"rc": "error"}, "data": []})
            data = LEGACY.get(path[len(prefix):])
            if data is None:
                return httpx.Response(404)
            return httpx.Response(200, json={"meta": {"rc": "ok"}, "data": data})
        return httpx.Response(404)

    t = httpx.MockTransport(handler)
    t.state = state  # type: ignore[attr-defined]
    return t
