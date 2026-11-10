-- Core schema. Every table carries "src" (which integration produced the row) so
-- later integrations share the same tables. MAC addresses are lower-case text.

CREATE TABLE collector_runs (
    id          bigserial PRIMARY KEY,
    src         text NOT NULL,
    started_at  timestamptz NOT NULL,
    finished_at timestamptz,
    ok          boolean,
    counts      jsonb,
    warnings    jsonb,
    error       text
);
CREATE INDEX ON collector_runs (src, started_at DESC);

-- ---------------------------------------------------------------- devices
CREATE TABLE devices (
    src         text NOT NULL,
    site        text NOT NULL,
    mac         text NOT NULL,
    name        text,
    type        text,            -- uap, usw, ugw, udm, uxg ...
    model       text,
    serial      text,
    ip          text,
    version     text,
    state       text,            -- online, offline, adopting, ...
    adopted     boolean,
    upgradable  boolean,
    uptime_s    bigint,
    uplink_mac  text,
    last_seen   timestamptz,
    updated_at  timestamptz NOT NULL,
    raw         jsonb,
    PRIMARY KEY (src, mac)
);

CREATE TABLE device_metrics (
    time         timestamptz NOT NULL,
    src          text NOT NULL,
    mac          text NOT NULL,
    cpu_pct      double precision,
    mem_pct      double precision,
    load_1       double precision,
    temperature  double precision,
    num_clients  integer,
    num_guests   integer,
    satisfaction integer,
    tx_bytes     bigint,
    rx_bytes     bigint
);
CREATE INDEX ON device_metrics (time DESC);
CREATE INDEX ON device_metrics (mac, time DESC);

-- ---------------------------------------------------------------- wireless
CREATE TABLE radio_metrics (
    time          timestamptz NOT NULL,
    src           text NOT NULL,
    device_mac    text NOT NULL,
    radio         text NOT NULL,   -- wifi0, wifi1 ...
    band          text,            -- 2.4, 5, 6
    channel       integer,
    width_mhz     integer,
    tx_power_dbm  integer,
    util_total    integer,         -- channel utilisation %
    util_self_rx  integer,
    util_self_tx  integer,
    num_clients   integer,
    satisfaction  integer,
    tx_retries    bigint,
    tx_packets    bigint
);
CREATE INDEX ON radio_metrics (time DESC);
CREATE INDEX ON radio_metrics (device_mac, radio, time DESC);

CREATE TABLE wlans (
    src        text NOT NULL,
    site       text NOT NULL,
    wlan_id    text NOT NULL,
    name       text,
    enabled    boolean,
    security   text,
    wpa_mode   text,
    wpa3       boolean,
    is_guest   boolean,
    hidden     boolean,
    bands      text,
    network_id text,
    updated_at timestamptz NOT NULL,
    raw        jsonb,              -- secrets (x_* keys) are stripped before storing
    PRIMARY KEY (src, wlan_id)
);

-- ---------------------------------------------------------------- switch ports
CREATE TABLE port_metrics (
    time        timestamptz NOT NULL,
    src         text NOT NULL,
    device_mac  text NOT NULL,
    port_idx    integer NOT NULL,
    name        text,
    up          boolean,
    speed_mbps  integer,
    full_duplex boolean,
    poe_w       double precision,
    tx_bytes    bigint,
    rx_bytes    bigint,
    tx_errors   bigint,
    rx_errors   bigint,
    tx_dropped  bigint,
    rx_dropped  bigint
);
CREATE INDEX ON port_metrics (time DESC);
CREATE INDEX ON port_metrics (device_mac, port_idx, time DESC);

-- ---------------------------------------------------------------- clients / users
CREATE TABLE clients (
    src          text NOT NULL,
    site         text NOT NULL,
    mac          text NOT NULL,
    hostname     text,
    name         text,            -- alias set in the controller
    display_name text,            -- best available label
    identity     text,            -- 802.1X identity, when present
    oui          text,
    os_name      text,
    dev_vendor   text,
    dev_family   text,
    is_wired     boolean,
    is_guest     boolean,
    blocked      boolean,
    fixed_ip     text,
    note         text,
    first_seen   timestamptz,
    last_seen    timestamptz,
    connected    boolean NOT NULL DEFAULT false,
    -- last observed attachment
    ip           text,
    ap_mac       text,
    sw_mac       text,
    sw_port      integer,
    essid        text,
    bssid        text,
    network      text,
    vlan         integer,
    updated_at   timestamptz NOT NULL,
    raw          jsonb,
    PRIMARY KEY (src, mac)
);
CREATE INDEX ON clients (connected);

CREATE TABLE client_metrics (
    time         timestamptz NOT NULL,
    src          text NOT NULL,
    mac          text NOT NULL,
    ip           text,
    is_wired     boolean,
    ap_mac       text,
    sw_mac       text,
    sw_port      integer,
    essid        text,
    band         text,
    radio_proto  text,
    channel      integer,
    signal_dbm   integer,
    noise_dbm    integer,
    rssi         integer,
    tx_rate_kbps bigint,
    rx_rate_kbps bigint,
    tx_bytes     bigint,
    rx_bytes     bigint,
    tx_bps       double precision,
    rx_bps       double precision,
    tx_retries   bigint,
    satisfaction integer,
    uptime_s     bigint,
    roam_count   integer
);
CREATE INDEX ON client_metrics (time DESC);
CREATE INDEX ON client_metrics (mac, time DESC);
CREATE INDEX ON client_metrics (ap_mac, time DESC);

-- ---------------------------------------------------------------- L3
CREATE TABLE networks (
    src        text NOT NULL,
    site       text NOT NULL,
    network_id text NOT NULL,
    name       text,
    purpose    text,
    vlan       integer,
    subnet     text,              -- network prefix, e.g. 192.168.10.0/24
    gateway_ip text,
    dhcp       boolean,
    domain     text,
    updated_at timestamptz NOT NULL,
    raw        jsonb,
    PRIMARY KEY (src, network_id)
);

CREATE TABLE routes (
    src        text NOT NULL,
    site       text NOT NULL,
    device_mac text NOT NULL,     -- router that holds the route
    prefix     text NOT NULL,
    next_hop   text NOT NULL DEFAULT '',
    interface  text NOT NULL DEFAULT '',
    protocol   text,              -- connected, static, kernel, ospf, bgp ...
    metric     integer,
    selected   boolean,
    last_seen  timestamptz NOT NULL,
    PRIMARY KEY (src, device_mac, prefix, next_hop, interface)
);

-- ---------------------------------------------------------------- topology
-- One row per observed adjacency. kind: uplink | lldp | client
CREATE TABLE topology_links (
    src        text NOT NULL,
    site       text NOT NULL,
    kind       text NOT NULL,
    a_id       text NOT NULL,     -- reporting end (device or client MAC)
    a_port     text NOT NULL DEFAULT '',
    b_id       text NOT NULL,     -- far end (MAC / LLDP chassis id)
    b_port     text NOT NULL DEFAULT '',
    medium     text,              -- wired | wireless
    speed_mbps integer,
    detail     jsonb,
    last_seen  timestamptz NOT NULL,
    PRIMARY KEY (src, kind, a_id, a_port, b_id, b_port)
);
CREATE INDEX ON topology_links (last_seen);

-- Map views: the API and Grafana's node-graph panel both read these, so the map is
-- always derived from what was most recently collected.
CREATE VIEW topology_nodes AS
SELECT d.mac AS id,
       COALESCE(d.name, d.mac) AS title,
       concat_ws(' · ', d.model, d.ip) AS subtitle,
       CASE WHEN d.type IN ('ugw', 'udm', 'uxg') THEN 'gateway'
            WHEN d.type = 'usw' THEN 'switch'
            WHEN d.type = 'uap' THEN 'ap'
            ELSE COALESCE(d.type, 'device') END AS kind,
       d.state AS mainstat
FROM devices d
UNION ALL
SELECT c.mac,
       COALESCE(c.display_name, c.mac),
       concat_ws(' · ', c.ip, c.essid),
       'client',
       CASE WHEN c.is_wired THEN 'wired' ELSE 'wifi' END
FROM clients c
WHERE c.connected AND NOT EXISTS (SELECT 1 FROM devices d WHERE d.mac = c.mac)
UNION ALL
-- far ends seen only through LLDP / uplink data (third-party gear)
SELECT n.id, n.id, 'discovered neighbor', 'neighbor', NULL
FROM (
    SELECT DISTINCT b_id AS id FROM topology_links
    WHERE last_seen > now() - interval '15 minutes' AND kind <> 'client'
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
       kind, medium, a_port, b_port, speed_mbps,
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
