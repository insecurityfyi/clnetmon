"""SNMP: parsing, discovery, storage, and the combined UniFi + SNMP map."""
import os
from datetime import datetime, timezone

import httpx
import pytest

import sample
import snmp_sample as ss
from nms import db
from nms.integrations.snmp import mib
from nms.integrations.snmp.collector import SnmpCollector, expand_cidrs
from nms.integrations.unifi.client import UnifiClient
from nms.integrations.unifi.collector import UnifiCollector

NOW = datetime(2026, 11, 10, 20, 0, tzinfo=timezone.utc)
DSN = os.environ.get("NMS_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="NMS_TEST_DATABASE_URL not set")


def sess(host):
    return ss.FakeSession(host, ss.COMMUNITY)


async def test_switch():
    s = await mib.poll_device(sess("192.168.7.10"), "192.168.7.10", NOW)
    d = s["device"]
    assert (d["mac"], d["name"], d["type"], d["uptime_s"]) == (ss.EDGE_SW, "edge-sw1", "switch", 1234)
    ports = {p["port_idx"]: p for p in s["ports"]}
    assert ports[1]["alias"] == "uplink to core" and ports[1]["speed_mbps"] == 1000
    assert ports[3]["up"] is False and ports[1]["tx_bytes"] == 2000 and ports[1]["rx_errors"] == 3
    links = {l["b_id"]: l for l in s["links"]}
    core = links[ss.UNIFI_SW]            # MAC chassis id -> joins the UniFi node
    assert (core["a_port"], core["b_port"], core["detail"]["sys_name"]) == ("1/1", "Port 9", "Core Switch")
    assert links["lldp:fw1"]["a_port"] == "1/2"   # non-MAC chassis id falls back to sysName
    fdb = {(f["mac"], f["vlan"]): f for f in s["fdb"]}
    assert fdb[(ss.PC, 1)]["port_name"] == "1/1" and fdb[(ss.PRN, 20)]["port_name"] == "1/3"
    assert s["warnings"] == [] and s["printer_metrics"] == []


async def test_firewall():
    s = await mib.poll_device(sess("192.168.7.1"), "192.168.7.1", NOW)
    assert s["device"]["type"] == "firewall" and s["device"]["mac"] == ss.FW
    r = {x["prefix"]: x for x in s["routes"]}
    assert (r["0.0.0.0/0"]["next_hop"], r["0.0.0.0/0"]["protocol"], r["0.0.0.0/0"]["interface"]) == \
           ("198.51.100.1", "static", "igb0")
    assert r["192.168.7.0/24"]["protocol"] == "connected" and r["192.168.7.0/24"]["next_hop"] == ""
    assert r["10.99.0.0/16"]["protocol"] == "ospf"
    assert {a["ip"]: a["mac"] for a in s["arp"]} == {"192.168.7.50": ss.PC, "192.168.7.60": ss.PRN}
    assert s["device_metrics"]["cpu_pct"] == 20.0 and s["device_metrics"]["mem_pct"] == 25.0


async def test_printer():
    s = await mib.poll_device(sess("192.168.7.60"), "192.168.7.60", NOW)
    d = s["device"]
    assert d["type"] == "printer" and d["model"] == "Example LaserJet Pro M404"
    assert d["raw"]["printer"] == {"status": "idle", "page_count": 48211, "display": "Ready"}
    sup = {x["name"]: x for x in s["printer_supplies"]}
    assert sup["Black Cartridge"]["level_pct"] == 37.0
    assert sup["Imaging Drum"]["level_pct"] is None and sup["Imaging Drum"]["level"] == -3


async def test_identity_merges_with_known_mac_and_old_tables():
    s = await mib.poll_device(sess("192.168.7.2"), "192.168.7.2", NOW,
                              frozenset({ss.UNIFI_SW}))
    assert s["device"]["mac"] == ss.UNIFI_SW          # not the lower-sorting port MAC
    assert [(r["prefix"], r["protocol"], r["interface"]) for r in s["routes"]] == \
           [("192.168.7.0/24", "connected", "Port 2")]
    assert [(f["mac"], f["vlan"], f["port_name"]) for f in s["fdb"]] == [(ss.EDGE_SW, 0, "Port 2")]


async def test_minimal_agent_and_garbage():
    agents = {"h": {**ss.system("bare", "thing"),
                    "1.3.6.1.2.1.2.2.1.6.1": (ss.STR, b"\x00" * 6),
                    "1.3.6.1.2.1.4.24.7.1.9.9.9": (ss.INT, 1),
                    "1.0.8802.1.1.2.1.4.1.1.5.0": (ss.STR, b"x")}}
    s = await mib.poll_device(ss.FakeSession("h", ss.COMMUNITY, agents=agents), "h", NOW)
    assert s["device"]["mac"] == "ip:h" and s["device"]["type"] == "host"
    assert s["routes"] == [] and s["links"] == []
    with pytest.raises(Exception):
        await mib.poll_device(ss.FakeSession("nope", ss.COMMUNITY), "nope", NOW)


def test_expand_cidrs():
    hosts, warn = expand_cidrs(["192.168.7.0/30", "10.0.0.0/8", "bogus", "192.168.7.1/32"])
    assert hosts == ["192.168.7.1", "192.168.7.2"] and len(warn) == 2


def make(targets=(), discovery=(), communities=("wrong", ss.COMMUNITY)):
    return SnmpCollector(list(communities), list(targets), list(discovery),
                         session_factory=ss.FakeSession)


async def test_probe_picks_working_community():
    col = make()
    assert await col.probe("192.168.7.60") == (1, "office-printer")
    assert await col.probe("192.168.7.77") is None
    found = await col.sweep(expand_cidrs(["192.168.7.0/24"])[0])
    assert sorted(found) == sorted(ss.AGENTS)


@pytest.fixture
async def pool():
    p = await db.create_pool(DSN)
    await p.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    assert await db.migrate(p) == ["001_init.sql", "002_snmp.sql"]
    yield p
    await p.close()


@needs_db
async def test_discovery_poll_and_combined_map(pool, monkeypatch):
    # UniFi first, so SNMP can recognise the UniFi switch by MAC
    uc = UnifiClient("https://c", sample.API_KEY, transport=sample.transport())
    await UnifiCollector(uc).run_once(pool)
    await uc.aclose()

    col = make(targets=["192.168.7.10", "10.1.1.1"], discovery=["192.168.7.0/24"])
    counts, warnings = await col.run_once(pool)
    assert counts["devices"] == 4 and counts["offline"] == 0 and counts["printers"] == 1
    assert warnings == []

    t = {r["host"]: r for r in await pool.fetch("SELECT * FROM snmp_targets")}
    assert t["192.168.7.10"]["origin"] == "static" and t["192.168.7.60"]["origin"] == "discovered"
    assert t["10.1.1.1"]["community_ref"] is None and t["10.1.1.1"]["last_error"]
    assert t["192.168.7.254"]["alias_of"] == "192.168.7.1"      # same firewall, second address
    assert len(t) == 6

    # second cycle: alias is not polled again, history accumulates, nothing duplicates
    counts, _ = await col.run_once(pool)
    assert counts["devices"] == 4
    assert await pool.fetchval("SELECT count(*) FROM devices WHERE src = 'snmp'") == 4
    assert await pool.fetchval("SELECT count(*) FROM printer_supplies") == 4
    assert await pool.fetchval(
        "SELECT count(*) FROM devices WHERE mac = $1", ss.UNIFI_SW) == 2   # unifi + snmp rows

    # a device stops answering
    monkeypatch.delitem(ss.AGENTS, "192.168.7.60")
    counts, warnings = await col.run_once(pool)
    assert counts["offline"] == 1 and any("192.168.7.60" in w for w in warnings)
    assert await pool.fetchval("SELECT state FROM devices WHERE ip = '192.168.7.60'") == "offline"

    from fastapi import FastAPI
    from nms.api.routes import router
    app = FastAPI()
    app.include_router(router)
    app.state.pool = pool
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as api:
        topo = (await api.get("/api/v1/topology?include_routes=false")).json()
        nodes = {n["id"]: n for n in topo["nodes"]}
        assert len(nodes) == len(topo["nodes"])                  # one node per id
        assert nodes[ss.UNIFI_SW]["title"] == "Core Switch"      # UniFi row wins
        assert nodes[ss.EDGE_SW]["kind"] == "switch" and nodes[ss.FW]["kind"] == "firewall"
        assert nodes[ss.PRN]["kind"] == "printer"
        assert nodes["lldp:fw1"]["title"] == "fw1" and nodes["lldp:fw1"]["kind"] == "neighbor"
        pairs = {frozenset((e["source"], e["target"])) for e in topo["edges"]}
        assert frozenset((ss.EDGE_SW, ss.UNIFI_SW)) in pairs     # third-party switch joined to UniFi
        assert len(pairs) == len(topo["edges"])

        routed = (await api.get("/api/v1/topology")).json()
        assert any(e["source"] == ss.FW and e["target"] == "net:10.99.0.0/16" for e in routed["edges"])

        pr = (await api.get("/api/v1/printers")).json()
        assert pr[0]["page_count"] == 48211 and len(pr[0]["supplies"]) == 2
        tg = (await api.get("/api/v1/snmp/targets")).json()
        assert sum(x["responding"] for x in tg) == 5
        loc = (await api.get("/api/v1/locate", params={"q": "192.168.7.50"})).json()
        assert loc["arp"][0]["mac"] == ss.PC and loc["arp"][0]["seen_by"] == "fw1"
        assert loc["switch_ports"][0]["switch"] == "edge-sw1"
        assert loc["clients"][0]["hostname"] == "workstation"
        dev = (await api.get(f"/api/v1/devices/{ss.UNIFI_SW}")).json()
        assert dev["sources"] == ["unifi", "snmp"]
    await col.aclose()
