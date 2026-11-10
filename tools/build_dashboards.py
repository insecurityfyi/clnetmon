"""Generates grafana/dashboards/*.json.  Run:  python tools/build_dashboards.py
Edit the queries here, not the JSON."""
import json
import os

OUT = os.path.join(os.path.dirname(__file__), "..", "grafana", "dashboards")
NAV = [{"type": "dashboards", "tags": ["nms"], "asDropdown": False, "title": "NMS",
        "includeVars": False, "keepTime": True, "icon": "external link"}]
DEVICE_URL = "/d/nms-device/device-detail?var-device=${__data.fields.MAC}&${__url_time_range}"


def link_column(column, title="Open device detail"):
    """Make a table column a link to the device detail dashboard (needs a MAC column)."""
    return {"matcher": {"id": "byName", "options": column},
            "properties": [{"id": "links", "value": [{"title": title, "url": DEVICE_URL}]}]}


def hide_column(column):
    return {"matcher": {"id": "byName", "options": column},
            "properties": [{"id": "custom.hidden", "value": True}]}


def unit_column(column, unit, decimals=None):
    props = [{"id": "unit", "value": unit}]
    if decimals is not None:
        props.append({"id": "decimals", "value": decimals})
    return {"matcher": {"id": "byName", "options": column}, "properties": props}


DS = {"type": "grafana-postgresql-datasource", "uid": "nms-pg"}
panels = []
def panel(title, type_, x, y, w, h, queries, **extra):
    targets = [{"refId": r, "datasource": DS, "rawQuery": True, "editorMode": "code",
                "format": f, "rawSql": " ".join(q.split())} for r, f, q in queries]
    p = {"id": len(panels) + 1, "title": title, "type": type_, "datasource": DS,
         "gridPos": {"x": x, "y": y, "w": w, "h": h}, "targets": targets}
    p.update(extra)
    panels.append(p)

def stat(title, x, sql):
    panel(title, "stat", x, 0, 4, 4, [("A", "table", sql)],
          options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                   "colorMode": "none", "graphMode": "none"})

stat("Devices online", 0, "SELECT count(*) FROM topology_devices WHERE state = 'online'")
stat("Devices not online", 4, "SELECT count(*) FROM topology_devices WHERE state IS DISTINCT FROM 'online'")
stat("Clients connected", 8, "SELECT count(*) FROM clients WHERE connected")
stat("Wireless clients", 12, "SELECT count(*) FROM clients WHERE connected AND NOT is_wired")
stat("Wired clients", 16, "SELECT count(*) FROM clients WHERE connected AND is_wired")
stat("Seconds since last poll", 20,
     "SELECT round(extract(epoch FROM now() - max(finished_at))) FROM collector_runs WHERE ok")

panel("Network map (uplinks, LLDP neighbors, routes)", "nodeGraph", 0, 4, 24, 16, [
    ("nodes", "table", """SELECT id, title, subtitle, mainstat, kind AS detail__kind
        FROM topology_nodes
        WHERE ('$show_clients' = 'yes' OR kind <> 'client')
          AND ('$show_routes' = 'yes' OR kind <> 'network')"""),
    ("edges", "table", """SELECT e.id, e.source, e.target, e.mainstat, e.kind AS detail__kind,
               NULLIF(e.a_port, '') AS detail__source_port, NULLIF(e.b_port, '') AS detail__target_port
        FROM topology_edges e
        WHERE ('$show_clients' = 'yes' OR e.kind <> 'client')
          AND ('$show_routes' = 'yes' OR e.kind <> 'route')
          AND (SELECT count(*) FROM topology_nodes n
               WHERE n.id IN (e.source, e.target)
                 AND ('$show_clients' = 'yes' OR n.kind <> 'client')
                 AND ('$show_routes' = 'yes' OR n.kind <> 'network')) = 2"""),
])

ts_opts = {"fieldConfig": {"defaults": {"custom": {"lineWidth": 1, "fillOpacity": 0,
                                                   "showPoints": "never", "spanNulls": True}},
                           "overrides": []},
           "options": {"legend": {"displayMode": "list", "placement": "bottom"},
                       "tooltip": {"mode": "multi", "sort": "desc"}}}
panel("Wireless clients per AP", "timeseries", 0, 20, 12, 8, [("A", "time_series", """
    SELECT $__timeGroup(m.time, $__interval) AS time, COALESCE(d.name, m.ap_mac) AS metric,
           count(DISTINCT m.mac) AS value
    FROM client_metrics m LEFT JOIN topology_devices d ON d.mac = m.ap_mac
    WHERE $__timeFilter(m.time) AND m.ap_mac IS NOT NULL
    GROUP BY 1, 2 ORDER BY 1""")], **ts_opts)
util = json.loads(json.dumps(ts_opts))
util["fieldConfig"]["defaults"].update({"unit": "percent", "min": 0, "max": 100})
panel("Channel utilization by radio", "timeseries", 12, 20, 12, 8, [("A", "time_series", """
    SELECT $__timeGroup(r.time, $__interval) AS time,
           COALESCE(d.name, r.device_mac) || ' ' || COALESCE(r.band, r.radio) || ' GHz' AS metric,
           avg(r.util_total) AS value
    FROM radio_metrics r LEFT JOIN topology_devices d ON d.mac = r.device_mac
    WHERE $__timeFilter(r.time)
    GROUP BY 1, 2 ORDER BY 1""")], **util)

panel("Connected clients", "table", 0, 28, 24, 10, [("A", "table", """
    SELECT c.display_name AS "Client", c.ip AS "IP", c.mac AS "MAC",
           CASE WHEN c.is_wired THEN 'wired' ELSE c.essid END AS "SSID",
           COALESCE(d.name, c.ap_mac, c.sw_mac) AS "Connected to", c.sw_port AS "Port",
           m.band AS "Band", m.channel AS "Ch", m.signal_dbm AS "Signal dBm",
           m.rx_rate_kbps / 1000 AS "Rx Mb/s", m.tx_rate_kbps / 1000 AS "Tx Mb/s",
           m.satisfaction AS "Experience", c.network AS "Network", c.oui AS "Vendor"
    FROM clients c
    LEFT JOIN topology_devices d ON d.mac = COALESCE(c.ap_mac, c.sw_mac)
    LEFT JOIN LATERAL (SELECT * FROM client_metrics m WHERE m.mac = c.mac
                       AND m.time > now() - interval '10 minutes'
                       ORDER BY time DESC LIMIT 1) m ON true
    WHERE c.connected ORDER BY m.signal_dbm NULLS LAST""")],
    options={"showHeader": True, "footer": {"show": False}})

panel("Devices", "table", 0, 38, 14, 8, [("A", "table", """
    SELECT d.name AS "Device", d.mac AS "MAC", d.type AS "Type", left(d.model, 40) AS "Model", d.ip AS "IP",
           d.state AS "State", d.version AS "Firmware",
           m.cpu_pct AS "CPU %", m.mem_pct AS "Mem %", m.num_clients AS "Clients",
           round(d.uptime_s / 86400.0, 1) AS "Uptime days",
           (SELECT string_agg(x.src, '+' ORDER BY x.src) FROM devices x WHERE x.mac = d.mac) AS "Source"
    FROM topology_devices d
    LEFT JOIN LATERAL (SELECT * FROM device_metrics m WHERE m.mac = d.mac AND m.cpu_pct IS NOT NULL
                       ORDER BY time DESC LIMIT 1) m ON true
    ORDER BY d.name""")],
    fieldConfig={"defaults": {}, "overrides": [link_column("Device"), hide_column("MAC")]})
panel("Routing table", "table", 14, 38, 10, 8, [("A", "table", """
    SELECT d.name AS "Router", r.prefix AS "Prefix", r.protocol AS "Type",
           NULLIF(r.next_hop, '') AS "Next hop", r.interface AS "Interface",
           n.name AS "Network", n.vlan AS "VLAN"
    FROM routes r LEFT JOIN networks n ON n.subnet = r.prefix
    LEFT JOIN topology_devices d ON d.mac = r.device_mac
    WHERE r.last_seen > now() - interval '15 minutes' ORDER BY d.name, r.prefix""")])

panel("Printers", "table", 0, 46, 12, 8, [("A", "table", """
    SELECT d.name AS "Printer", d.ip AS "IP", d.state AS "State",
           d.raw->'printer'->>'status' AS "Status", s.name AS "Supply",
           s.level_pct AS "Level %", (d.raw->'printer'->>'page_count')::bigint AS "Pages printed"
    FROM devices d
    LEFT JOIN LATERAL (SELECT DISTINCT ON (name) name, level_pct FROM printer_supplies p
                       WHERE p.device_mac = d.mac AND p.time > now() - interval '1 day'
                       ORDER BY name, time DESC) s ON true
    WHERE d.type = 'printer' ORDER BY d.name, s.name""")])
panel("SNMP targets (static and discovered)", "table", 12, 46, 12, 8, [("A", "table", """
    SELECT t.host AS "Address", t.sys_name AS "Name", d.type AS "Type", t.origin AS "Found by",
           CASE WHEN t.alias_of IS NOT NULL THEN 'same device as ' || t.alias_of
                WHEN t.community_ref IS NULL THEN 'no answer'
                WHEN t.last_error IS NOT NULL THEN 'failing' ELSE 'ok' END AS "Status",
           t.last_ok AS "Last answered", t.last_error AS "Last error"
    FROM snmp_targets t LEFT JOIN devices d ON d.src = 'snmp' AND d.mac = t.device_mac
    ORDER BY t.host""")])

def toggle(name, label, default):
    return {"name": name, "label": label, "type": "custom", "query": "no,yes",
            "current": {"text": default, "value": default, "selected": True},
            "options": [{"text": v, "value": v, "selected": v == default} for v in ("no", "yes")]}

def save(uid, title, tags, variables, version, time_from="now-6h"):
    dash = {"uid": uid, "title": title, "tags": tags, "schemaVersion": 39, "version": version,
            "editable": True, "refresh": "1m", "time": {"from": time_from, "to": "now"},
            "timezone": "browser", "links": NAV, "templating": {"list": variables},
            "panels": list(panels)}
    with open(os.path.join(OUT, uid + ".json"), "w") as f:
        json.dump(dash, f, indent=2)
        f.write("\n")
    print(f"{uid}: {len(panels)} panels")
    panels.clear()


save("nms-overview", "NMS overview", ["nms"],
     [toggle("show_clients", "Clients on map", "no"), toggle("show_routes", "Routes on map", "yes")], 4)

# =====================================================================================
# Devices and interfaces
# =====================================================================================
ROUTERS = "('firewall', 'router', 'ugw', 'udm', 'uxg')"
bps = json.loads(json.dumps(ts_opts))
bps["fieldConfig"]["defaults"].update({"unit": "bps", "min": 0})
bps["options"]["legend"] = {"displayMode": "table", "placement": "right", "calcs": ["lastNotNull", "max"],
                            "sortBy": "Last *", "sortDesc": True}


def fw_traffic(title, x, col):
    panel(title, "timeseries", x, 0, 12, 9, [("A", "time_series", f"""
        SELECT p.time, COALESCE(d.name, p.device_mac) || ' ' || p.name
                 || COALESCE(' (' || NULLIF(p.alias, '') || ')', '') AS metric,
               p.{col} AS value
        FROM port_metrics p
        JOIN device_port_src s ON s.mac = p.device_mac AND s.src = p.src
        JOIN topology_devices d ON d.mac = p.device_mac
        WHERE $__timeFilter(p.time) AND d.type IN {ROUTERS} AND p.{col} IS NOT NULL
          AND EXISTS (SELECT 1 FROM port_latest l WHERE l.device_mac = p.device_mac
                      AND l.port_idx = p.port_idx AND l.up AND COALESCE(l.rx_bps, 0) + COALESCE(l.tx_bps, 0) > 0)
        ORDER BY 1""")], **bps)


fw_traffic("Firewall and gateway interfaces: traffic in", 0, "rx_bps")
fw_traffic("Firewall and gateway interfaces: traffic out", 12, "tx_bps")

panel("Devices (click a name for full telemetry)", "table", 0, 9, 24, 10, [("A", "table", """
    SELECT d.name AS "Device", d.mac AS "MAC", d.type AS "Type", left(d.model, 40) AS "Model",
           d.ip AS "IP", d.state AS "State", m.cpu_pct AS "CPU", m.mem_pct AS "Memory",
           t.up AS "Ports up", t.total AS "Ports", t.rx AS "In", t.tx AS "Out",
           d.uptime_s AS "Uptime"
    FROM topology_devices d
    LEFT JOIN LATERAL (SELECT cpu_pct, mem_pct FROM device_metrics m
                       WHERE m.mac = d.mac AND m.cpu_pct IS NOT NULL
                         AND m.time > now() - interval '15 minutes'
                       ORDER BY time DESC LIMIT 1) m ON true
    LEFT JOIN LATERAL (SELECT count(*) FILTER (WHERE up) AS up, count(*) AS total,
                              sum(rx_bps) AS rx, sum(tx_bps) AS tx
                       FROM port_latest l WHERE l.device_mac = d.mac) t ON true
    ORDER BY (d.type IN """ + ROUTERS + """) DESC, d.type, d.name""")],
    fieldConfig={"defaults": {}, "overrides": [
        link_column("Device"), hide_column("MAC"), unit_column("CPU", "percent", 0),
        unit_column("Memory", "percent", 0), unit_column("In", "bps"), unit_column("Out", "bps"),
        unit_column("Uptime", "dtdurations")]})

panel("Busiest interfaces right now", "table", 0, 19, 14, 10, [("A", "table", """
    SELECT d.name AS "Device", d.mac AS "MAC", l.name AS "Interface", NULLIF(l.alias, '') AS "Description",
           l.rx_bps AS "In", l.tx_bps AS "Out", l.speed_mbps AS "Speed Mb",
           round((100 * GREATEST(l.rx_bps, l.tx_bps) / NULLIF(l.speed_mbps * 1e6, 0))::numeric, 1) AS "Utilization"
    FROM port_latest l JOIN topology_devices d ON d.mac = l.device_mac
    WHERE l.up AND COALESCE(l.rx_bps, 0) + COALESCE(l.tx_bps, 0) > 0
    ORDER BY COALESCE(l.rx_bps, 0) + COALESCE(l.tx_bps, 0) DESC LIMIT 25""")],
    fieldConfig={"defaults": {}, "overrides": [
        link_column("Device"), hide_column("MAC"), unit_column("In", "bps"),
        unit_column("Out", "bps"), unit_column("Utilization", "percent")]})
panel("Interfaces with errors or discards", "table", 14, 19, 10, 10, [("A", "table", """
    SELECT d.name AS "Device", d.mac AS "MAC", l.name AS "Interface", l.errors_ps AS "Per second",
           COALESCE(l.rx_errors, 0) + COALESCE(l.tx_errors, 0) AS "Errors total",
           COALESCE(l.rx_dropped, 0) + COALESCE(l.tx_dropped, 0) AS "Discards total"
    FROM port_latest l JOIN topology_devices d ON d.mac = l.device_mac
    WHERE l.errors_ps > 0 ORDER BY l.errors_ps DESC LIMIT 25""")],
    fieldConfig={"defaults": {}, "overrides": [link_column("Device"), hide_column("MAC"),
                                                unit_column("Per second", "none", 2)]})
save("nms-devices", "Devices and interfaces", ["nms"], [], 1)

# =====================================================================================
# Device detail (opened by clicking a device; $device is its MAC)
# =====================================================================================
DEV = "'$device'"


def dstat(title, x, w, sql, unit=None, text=False):
    opts = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "/.*/" if text else "", "values": False},
            "colorMode": "none", "graphMode": "none"}
    extra = {"options": opts}
    if unit:
        extra["fieldConfig"] = {"defaults": {"unit": unit}, "overrides": []}
    panel(title, "stat", x, 0, w, 4, [("A", "table", sql)], **extra)


dstat("Device", 0, 6, f"SELECT COALESCE(name, mac) FROM topology_devices WHERE mac = {DEV}", text=True)
dstat("Type / address", 6, 4, f"SELECT concat_ws(' · ', type, ip) FROM topology_devices WHERE mac = {DEV}", text=True)
dstat("State", 10, 2, f"SELECT state FROM topology_devices WHERE mac = {DEV}", text=True)
dstat("Uptime", 12, 3, f"SELECT uptime_s FROM topology_devices WHERE mac = {DEV}", unit="dtdurations")
dstat("CPU", 15, 3, f"""SELECT cpu_pct FROM device_metrics WHERE mac = {DEV} AND cpu_pct IS NOT NULL
                       AND time > now() - interval '15 minutes' ORDER BY time DESC LIMIT 1""", unit="percent")
dstat("Memory", 18, 3, f"""SELECT mem_pct FROM device_metrics WHERE mac = {DEV} AND mem_pct IS NOT NULL
                          AND time > now() - interval '15 minutes' ORDER BY time DESC LIMIT 1""", unit="percent")
dstat("Ports up", 21, 3, f"""SELECT count(*) FILTER (WHERE up) || ' / ' || count(*)
                            FROM port_latest WHERE device_mac = {DEV}""", text=True)

pct = json.loads(json.dumps(ts_opts))
pct["fieldConfig"]["defaults"].update({"unit": "percent", "min": 0, "max": 100})
panel("CPU and memory", "timeseries", 0, 4, 12, 8, [
    ("A", "time_series", f"""SELECT $__timeGroup(time, $__interval) AS time, 'CPU' AS metric, avg(cpu_pct) AS value
        FROM device_metrics WHERE mac = {DEV} AND $__timeFilter(time) AND cpu_pct IS NOT NULL
        GROUP BY 1 ORDER BY 1"""),
    ("B", "time_series", f"""SELECT $__timeGroup(time, $__interval) AS time, 'Memory' AS metric, avg(mem_pct) AS value
        FROM device_metrics WHERE mac = {DEV} AND $__timeFilter(time) AND mem_pct IS NOT NULL
        GROUP BY 1 ORDER BY 1""")], **pct)
other = json.loads(json.dumps(ts_opts))
panel("Clients, temperature, experience", "timeseries", 12, 4, 12, 8, [("A", "time_series", f"""
    SELECT $__timeGroup(time, $__interval) AS time, avg(num_clients) AS "Clients",
           avg(temperature) AS "Temperature C", avg(satisfaction) AS "Experience %"
    FROM device_metrics WHERE mac = {DEV} AND $__timeFilter(time)
    GROUP BY 1 ORDER BY 1""")], **other)


def if_series(title, x, y, col, opts):
    panel(title, "timeseries", x, y, 12, 9, [("A", "time_series", f"""
        SELECT p.time, p.name || COALESCE(' (' || NULLIF(p.alias, '') || ')', '') AS metric,
               p.{col} AS value
        FROM port_metrics p JOIN device_port_src s ON s.mac = p.device_mac AND s.src = p.src
        WHERE p.device_mac = {DEV} AND $__timeFilter(p.time) AND p.name IN ($interface)
          AND p.{col} IS NOT NULL
        ORDER BY 1""")], **opts)


if_series("Interface traffic in", 0, 12, "rx_bps", bps)
if_series("Interface traffic out", 12, 12, "tx_bps", bps)
errs = json.loads(json.dumps(ts_opts))
errs["fieldConfig"]["defaults"].update({"unit": "none", "min": 0})
if_series("Interface errors and discards per second", 0, 21, "errors_ps", errs)
radio = json.loads(json.dumps(ts_opts))
radio["fieldConfig"]["defaults"].update({"unit": "percent", "min": 0, "max": 100})
panel("Wi-Fi channel utilization (access points)", "timeseries", 12, 21, 12, 9, [("A", "time_series", f"""
    SELECT $__timeGroup(time, $__interval) AS time, COALESCE(band, radio) || ' GHz ch ' || max(channel) AS metric,
           avg(util_total) AS value
    FROM radio_metrics WHERE device_mac = {DEV} AND $__timeFilter(time)
    GROUP BY 1, band, radio ORDER BY 1""")], **radio)

panel("Interfaces", "table", 0, 30, 24, 11, [("A", "table", f"""
    SELECT l.port_idx AS "#", l.name AS "Interface", NULLIF(l.alias, '') AS "Description",
           CASE WHEN l.up THEN 'up' WHEN l.admin_up IS FALSE THEN 'disabled' ELSE 'down' END AS "Status",
           l.speed_mbps AS "Speed Mb", l.rx_bps AS "In", l.tx_bps AS "Out",
           round((100 * GREATEST(l.rx_bps, l.tx_bps) / NULLIF(l.speed_mbps * 1e6, 0))::numeric, 1) AS "Utilization",
           l.rx_bytes AS "Received", l.tx_bytes AS "Sent", l.errors_ps AS "Errors/s", l.poe_w AS "PoE W",
           (SELECT string_agg(COALESCE(n.title, k.b_id) || COALESCE(' ' || NULLIF(k.b_port, ''), ''), ', ')
            FROM topology_links k LEFT JOIN topology_nodes n ON n.id = k.b_id
            WHERE k.a_id = l.device_mac AND k.kind <> 'client'
              AND k.a_port IN (l.name, l.port_idx::text)
              AND k.last_seen > now() - interval '15 minutes') AS "Neighbor"
    FROM port_latest l WHERE l.device_mac = {DEV} ORDER BY l.port_idx""")],
    fieldConfig={"defaults": {}, "overrides": [
        unit_column("In", "bps"), unit_column("Out", "bps"), unit_column("Utilization", "percent"),
        unit_column("Received", "decbytes"), unit_column("Sent", "decbytes"),
        unit_column("Errors/s", "none", 2)]})

panel("Neighbors (LLDP and uplinks)", "table", 0, 41, 12, 8, [("A", "table", f"""
    SELECT CASE WHEN k.a_id = {DEV} THEN NULLIF(k.a_port, '') ELSE NULLIF(k.b_port, '') END AS "Local port",
           COALESCE(n.title, CASE WHEN k.a_id = {DEV} THEN k.b_id ELSE k.a_id END) AS "Neighbor",
           CASE WHEN k.a_id = {DEV} THEN NULLIF(k.b_port, '') ELSE NULLIF(k.a_port, '') END AS "Neighbor port",
           k.kind AS "Seen via", k.src AS "Source", k.medium AS "Medium"
    FROM topology_links k
    LEFT JOIN topology_nodes n ON n.id = CASE WHEN k.a_id = {DEV} THEN k.b_id ELSE k.a_id END
    WHERE {DEV} IN (k.a_id, k.b_id) AND k.kind <> 'client'
      AND k.last_seen > now() - interval '15 minutes'
    ORDER BY 1""")])
panel("Routing table", "table", 12, 41, 12, 8, [("A", "table", f"""
    SELECT r.prefix AS "Prefix", r.protocol AS "Type", NULLIF(r.next_hop, '') AS "Next hop",
           r.interface AS "Interface", r.metric AS "Metric"
    FROM routes r WHERE r.device_mac = {DEV} AND r.last_seen > now() - interval '15 minutes'
    ORDER BY r.prefix""")])
panel("Hosts on this device (ARP and MAC tables, connected clients)", "table", 0, 49, 16, 9, [("A", "table", f"""
    SELECT COALESCE(c.display_name, a.ip, f.mac) AS "Host", COALESCE(a.ip, c.ip) AS "IP", f.mac AS "MAC",
           f.port_name AS "Port", NULLIF(f.vlan, 0) AS "VLAN", c.oui AS "Vendor"
    FROM fdb_entries f
    LEFT JOIN LATERAL (SELECT ip FROM arp_entries a WHERE a.mac = f.mac ORDER BY last_seen DESC LIMIT 1) a ON true
    LEFT JOIN clients c ON c.mac = f.mac
    WHERE f.device_mac = {DEV} AND f.last_seen > now() - interval '15 minutes'
    UNION ALL
    SELECT c.display_name, c.ip, c.mac, COALESCE(c.sw_port::text, c.essid), c.vlan, c.oui
    FROM clients c WHERE c.connected AND {DEV} IN (c.ap_mac, c.sw_mac)
      AND NOT EXISTS (SELECT 1 FROM fdb_entries f WHERE f.device_mac = {DEV} AND f.mac = c.mac)
    ORDER BY 4, 1 LIMIT 500""")])
panel("Printer supplies", "bargauge", 16, 49, 8, 9, [("A", "table", f"""
    SELECT DISTINCT ON (name) name AS metric, level_pct AS value
    FROM printer_supplies WHERE device_mac = {DEV} AND time > now() - interval '1 day'
      AND level_pct IS NOT NULL
    ORDER BY name, time DESC""")],
    options={"orientation": "horizontal", "displayMode": "basic",
             "reduceOptions": {"calcs": ["lastNotNull"], "fields": "/^value$/", "values": True}},
    fieldConfig={"defaults": {"unit": "percent", "min": 0, "max": 100,
                              "displayName": "${__data.fields.metric}"}, "overrides": []})

device_var = {
    "name": "device", "label": "Device", "type": "query", "datasource": DS, "refresh": 1,
    "sort": 0, "multi": False, "includeAll": False,
    "query": "SELECT COALESCE(name, mac) || COALESCE(' (' || ip || ')', '') AS __text, mac AS __value "
             "FROM topology_devices ORDER BY (type IN " + ROUTERS + ") DESC, name"}
iface_var = {
    "name": "interface", "label": "Interfaces", "type": "query", "datasource": DS, "refresh": 2,
    "sort": 0, "multi": True, "includeAll": True, "current": {"text": "All", "value": "$__all"},
    "query": "SELECT name FROM port_latest WHERE device_mac = '$device' "
             "AND (up OR COALESCE(rx_bps, 0) + COALESCE(tx_bps, 0) > 0) ORDER BY port_idx"}
save("nms-device", "Device detail", ["nms"], [device_var, iface_var], 1)
