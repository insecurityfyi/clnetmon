-- Interface rates are computed when a sample is stored (delta against the previous
-- sample), so graphs and tables read them directly.
ALTER TABLE port_metrics
    ADD COLUMN rx_bps    double precision,   -- bits/s received
    ADD COLUMN tx_bps    double precision,   -- bits/s sent
    ADD COLUMN errors_ps double precision;   -- errors + discards per second, both directions

-- Which source's interface data to show for a device seen by more than one:
-- SNMP lists every interface (VLANs, tunnels, aggregates), so it wins when present.
CREATE VIEW device_port_src AS
SELECT mac, CASE WHEN bool_or(src = 'snmp') THEN 'snmp' ELSE min(src) END AS src
FROM devices GROUP BY mac;

-- Latest sample of every interface, one source per device.
CREATE VIEW port_latest AS
SELECT DISTINCT ON (p.device_mac, p.port_idx) p.*
FROM port_metrics p
JOIN device_port_src s ON s.mac = p.device_mac AND s.src = p.src
WHERE p.time > now() - interval '15 minutes'
ORDER BY p.device_mac, p.port_idx, p.time DESC;
