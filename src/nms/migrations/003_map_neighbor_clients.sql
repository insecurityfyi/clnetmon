-- A connected client that also shows up as an LLDP/uplink neighbour (NAS, unmanaged
-- switch, AP ...) is part of the infrastructure map: show it as a neighbour node so its
-- links never point at a node that the "clients" toggle has hidden.
CREATE OR REPLACE VIEW topology_nodes AS
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
       CASE WHEN EXISTS (SELECT 1 FROM topology_links l
                         WHERE l.kind <> 'client' AND c.mac IN (l.a_id, l.b_id)
                           AND l.last_seen > now() - interval '15 minutes')
            THEN 'neighbor' ELSE 'client' END,
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
