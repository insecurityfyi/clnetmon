"""Every query in every dashboard must execute, and interface rates must be right."""
import glob
import json
import os
import re
from datetime import datetime, timedelta, timezone

import pytest

import sample
import snmp_sample as ss
from nms import db
from nms.integrations.snmp.collector import SnmpCollector
from nms.integrations.unifi.client import UnifiClient
from nms.integrations.unifi.collector import UnifiCollector

DSN = os.environ.get("NMS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="NMS_TEST_DATABASE_URL not set")
DASH_DIR = os.path.join(os.path.dirname(__file__), "..", "grafana", "dashboards")


@pytest.fixture
async def pool():
    p = await db.create_pool(DSN)
    await p.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    await db.migrate(p)
    yield p
    await p.close()


def render(sql: str, device: str) -> str:
    sql = re.sub(r"\$__timeGroup\(([^,]+), \$__interval\)", r"date_trunc('minute', \1)", sql)
    sql = re.sub(r"\$__timeFilter\(([^)]+)\)", r"\1 > now() - interval '6 hours'", sql)
    return (sql.replace("$device", device).replace("$interface", "'1/1','1/2','igb0'")
            .replace("$show_clients", "yes").replace("$show_routes", "yes"))


async def test_port_rates(pool):
    t0 = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)

    def row(t, rx, tx, err):
        return {"time": t, "src": "snmp", "device_mac": "m", "port_idx": 1, "name": "p1", "up": True,
                "speed_mbps": 1000, "full_duplex": None, "poe_w": None, "tx_bytes": tx,
                "rx_bytes": rx, "tx_errors": err, "rx_errors": 0, "tx_dropped": 0, "rx_dropped": 0}

    samples = [(t0, 1_000, 5_000, 0),
               (t0 + timedelta(seconds=60), 1_000 + 7_500_000, 5_000 + 750_000, 120),   # 1 Mb/s in
               (t0 + timedelta(seconds=120), 500, 5_000 + 750_000, 120),                # counter reset
               (t0 + timedelta(hours=2), 9_000, 9_000_000, 121)]                        # gap too long
    async with pool.acquire() as conn:
        for t, rx, tx, err in samples:
            await db.insert_rows(conn, "port_metrics", [row(t, rx, tx, err)])
            await db.compute_port_rates(conn, "snmp", t)
    got = await pool.fetch("SELECT rx_bps, tx_bps, errors_ps FROM port_metrics ORDER BY time")
    assert [tuple(r) for r in got] == [
        (None, None, None), (1_000_000.0, 100_000.0, 2.0), (None, 0.0, 0.0), (None, None, None)]


async def test_every_dashboard_query_runs(pool, monkeypatch):
    uc = UnifiClient("https://c", sample.API_KEY, transport=sample.transport())
    unifi = UnifiCollector(uc)
    snmp = SnmpCollector(["x", ss.COMMUNITY], [], ["192.168.7.0/24"], session_factory=ss.FakeSession)
    for _ in range(2):
        await unifi.run_once(pool)
        await snmp.run_once(pool)
    await uc.aclose()
    # rates exist after the second cycle (sample counters are constant, so 0 rather than NULL)
    assert await pool.fetchval("SELECT count(*) FROM port_metrics WHERE rx_bps IS NOT NULL") > 0
    # one interface source per device, even for the switch polled by both UniFi and SNMP
    assert await pool.fetchval(
        "SELECT count(DISTINCT src) FROM port_latest WHERE device_mac = $1", ss.UNIFI_SW) == 1

    files = sorted(glob.glob(os.path.join(DASH_DIR, "*.json")))
    assert len(files) == 3
    ran = 0
    for device in (ss.FW, ss.UNIFI_SW, ss.PRN, "aa:bb:cc:00:00:03"):   # firewall, switch, printer, AP
        for f in files:
            dash = json.load(open(f))
            queries = [(p["title"], t["rawSql"]) for p in dash["panels"] for t in p["targets"]]
            queries += [("variable " + v["name"], v["query"]) for v in dash["templating"]["list"]
                        if v["type"] == "query"]
            for title, sql in queries:
                try:
                    await pool.fetch(render(sql, device))
                except Exception as e:
                    pytest.fail(f"{os.path.basename(f)} / {title}: {e}")
                ran += 1
    assert ran > 100

    # spot-check content of the device detail page for the firewall
    detail = {p["title"]: p for p in json.load(open(os.path.join(DASH_DIR, "nms-device.json")))["panels"]}
    rows = await pool.fetch(render(detail["Interfaces"]["targets"][0]["rawSql"], ss.FW))
    assert [r["Interface"] for r in rows] == ["igb0", "igb1"] and rows[0]["Description"] == "WAN"
    rows = await pool.fetch(render(detail["Interfaces"]["targets"][0]["rawSql"], ss.EDGE_SW))
    assert {r["Interface"]: r["Neighbor"] for r in rows}["1/1"] == "Core Switch Port 9"
    rows = await pool.fetch(render(detail["Routing table"]["targets"][0]["rawSql"], ss.FW))
    assert len(rows) == 3
    rows = await pool.fetch(render(detail["Printer supplies"]["targets"][0]["rawSql"], ss.PRN))
    assert [tuple(r) for r in rows] == [("Black Cartridge", 37.0)]
    devices = json.load(open(os.path.join(DASH_DIR, "nms-devices.json")))
    table = next(p for p in devices["panels"] if p["title"].startswith("Devices"))
    rows = await pool.fetch(render(table["targets"][0]["rawSql"], ss.FW))
    assert rows[0]["Type"] == "firewall" and len(rows) == len({r["MAC"] for r in rows}) == 7
    await snmp.aclose()
