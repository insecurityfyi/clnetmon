-- SNMP integration: targets (static + discovered), ARP and MAC tables, printers.
-- SNMP devices reuse devices / device_metrics / port_metrics / routes / topology_links
-- with src = 'snmp'.

CREATE TABLE snmp_targets (
    host          text PRIMARY KEY,
    origin        text NOT NULL,          -- static | discovered
    community_ref integer,                -- index into NMS_SNMP_COMMUNITIES; NULL = no answer yet
    device_mac    text,                   -- device id it resolved to
    alias_of      text,                   -- another address of a device already polled
    sys_name      text,
    first_seen    timestamptz NOT NULL DEFAULT now(),
    last_ok       timestamptz,
    last_error    text
);

ALTER TABLE port_metrics ADD COLUMN alias text, ADD COLUMN admin_up boolean;

CREATE TABLE arp_entries (
    src        text NOT NULL,
    device_mac text NOT NULL,             -- router/firewall that holds the entry
    ip         text NOT NULL,
    mac        text NOT NULL,
    interface  text,
    last_seen  timestamptz NOT NULL,
    PRIMARY KEY (src, device_mac, ip)
);
CREATE INDEX ON arp_entries (mac);

CREATE TABLE fdb_entries (
    src        text NOT NULL,
    device_mac text NOT NULL,             -- switch that learned the address
    mac        text NOT NULL,
    vlan       integer NOT NULL DEFAULT 0,
    port_idx   integer,
    port_name  text,
    last_seen  timestamptz NOT NULL,
    PRIMARY KEY (src, device_mac, mac, vlan)
);
CREATE INDEX ON fdb_entries (mac);

CREATE TABLE printer_metrics (
    time       timestamptz NOT NULL,
    src        text NOT NULL,
    device_mac text NOT NULL,
    status     text,
    page_count bigint
);
CREATE INDEX ON printer_metrics (device_mac, time DESC);

CREATE TABLE printer_supplies (
    time         timestamptz NOT NULL,
    src          text NOT NULL,
    device_mac   text NOT NULL,
    name         text NOT NULL,
    level        integer,
    max_capacity integer,
    level_pct    double precision
);
CREATE INDEX ON printer_supplies (device_mac, name, time DESC);

-- Map views, now source-aware:
--  * a device seen by both UniFi and SNMP is one node (UniFi's row wins)
--  * LLDP neighbours get their advertised system name as the title
--  * a managed device that is also a client of a switch/AP keeps that link ("access")
DROP VIEW topology_edges;
DROP VIEW topology_nodes;

CREATE VIEW topology_devices AS
SELECT DISTINCT ON (mac) *
FROM devices
ORDER BY mac, (src = 'unifi') DESC, updated_at DESC;

CREATE VIEW topology_nodes AS
SELECT d.mac AS id,
       COALESCE(d.name, d.mac) AS title,
       concat_ws(' · ', left(d.model, 40), d.ip) AS subtitle,
       CASE WHEN d.type IN ('ugw', 'udm', 'uxg') THEN 'gateway'
            WHEN d.type = 'usw' THEN 'switch'
            WHEN d.type = 'uap' THEN 'ap'
            ELSE COALESCE(d.type, 'device') END AS kind,
       d.state AS mainstat
FROM topology_devices d
UNION ALL
SELECT c.mac,
       COALESCE(c.display_name, c.mac),
       concat_ws(' · ', c.ip, c.essid),
       'client',
       CASE WHEN c.is_wired THEN 'wired' ELSE 'wifi' END
FROM clients c
WHERE c.connected AND NOT EXISTS (SELECT 1 FROM devices d WHERE d.mac = c.mac)
UNION ALL
SELECT n.id, COALESCE(n.sys_name, n.id), 'discovered neighbor', 'neighbor', NULL
FROM (
    SELECT b_id AS id, max(detail->>'sys_name') AS sys_name FROM topology_links
    WHERE last_seen > now() - interval '15 minutes' AND kind <> 'client'
    GROUP BY b_id
) n
WHERE NOT EXISTS (SELECT 1 FROM devices d WHERE d.mac = n.id)
  AND NOT EXISTS (SELECT 1 FROM clients c WHERE c.mac = n.id AND c.connected)
UNION ALL
SELECT 'net:' || p.prefix,
       CASE WHEN p.prefix IN ('0.0.0.0/0', '::/0') THEN 'Default route' ELSE p.prefix END,
       COALESCE((SELECT concat_ws(' · ', nw.name, 'VLAN ' || nw.vlan)
                 FROM networks nw WHERE nw.subnet = p.prefix LIMIT 1), 'routed prefix'),
       'network',
       NULL
FROM (SELECT DISTINCT prefix FROM routes
      WHERE last_seen > now() - interval '15 minutes') p;

CREATE VIEW topology_edges AS
WITH fresh AS (
    SELECT l.*,
           LEAST(a_id, b_id)    AS lo,
           GREATEST(a_id, b_id) AS hi
    FROM topology_links l
    WHERE last_seen > now() - interval '15 minutes'
), ranked AS (
    SELECT *, row_number() OVER (
               PARTITION BY lo, hi
               ORDER BY CASE kind WHEN 'uplink' THEN 0 WHEN 'lldp' THEN 1 ELSE 2 END,
                        last_seen DESC) AS rn
    FROM fresh
), candidate AS (
SELECT lo || '~' || hi AS id,
       a_id AS source, b_id AS target,
       CASE WHEN kind = 'client' AND EXISTS (SELECT 1 FROM devices d WHERE d.mac = ranked.a_id)
            THEN 'access' ELSE kind END AS kind,
       medium, a_port, b_port, speed_mbps,
       CASE WHEN medium = 'wireless' THEN 'wireless'
            WHEN speed_mbps IS NOT NULL THEN speed_mbps || ' Mb'
            ELSE COALESCE(medium, kind) END AS mainstat
FROM ranked WHERE rn = 1
UNION ALL
SELECT r.device_mac || '~net:' || r.prefix AS id,
       r.device_mac AS source, 'net:' || r.prefix AS target,
       'route' AS kind, NULL AS medium, r.interface AS a_port, '' AS b_port, NULL AS speed_mbps,
       COALESCE(r.protocol, 'route')
         || CASE WHEN r.next_hop <> '' THEN ' via ' || r.next_hop ELSE '' END AS mainstat
FROM (
    SELECT DISTINCT ON (device_mac, prefix) *
    FROM routes
    WHERE last_seen > now() - interval '15 minutes'
    ORDER BY device_mac, prefix, selected DESC NULLS LAST, metric NULLS LAST
) r
)
SELECT e.* FROM candidate e
WHERE EXISTS (SELECT 1 FROM topology_nodes n WHERE n.id = e.source)
  AND EXISTS (SELECT 1 FROM topology_nodes n WHERE n.id = e.target);
