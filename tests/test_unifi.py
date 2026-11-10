"""End-to-end: fake console -> collector -> PostgreSQL -> REST API.

Needs a scratch database: export NMS_TEST_DATABASE_URL (the schema "public" is wiped).
"""
import json
import os
from datetime import datetime, timezone

import httpx
import pytest

import sample
from nms import db
from nms.integrations.unifi import normalize as nz
from nms.integrations.unifi.client import UnifiClient, UnifiError
from nms.integrations.unifi.collector import UnifiCollector

NOW = datetime(2026, 11, 10, 18, 0, tzinfo=timezone.utc)
DSN = os.environ.get("NMS_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="NMS_TEST_DATABASE_URL not set")


def make_client(**kw):
    creds = kw.pop("creds", False)
    return UnifiClient("https://console", sample.API_KEY, transport=sample.transport(**kw),
                       username="ro" if creds else "", password="pw" if creds else "")


# ---------------------------------------------------------------- normalizers (no I/O)
def test_devices_radios_ports_links():
    out = nz.devices(sample.DEVICES, "default", NOW)
    by_mac = {d["mac"]: d for d in out["devices"]}
    gw = by_mac["aa:bb:cc:00:00:01"]          # MAC lower-cased
    assert gw["state"] == "online" and gw["type"] == "udm" and gw["uptime_s"] == 864000
    assert "x_authkey" not in gw["raw"]         # secrets stripped
    m = {r["mac"]: r for r in out["device_metrics"]}
    assert m["aa:bb:cc:00:00:01"]["cpu_pct"] == 12.5
    assert m["aa:bb:cc:00:00:02"]["temperature"] == 52.0

    radios = {(r["device_mac"], r["radio"]): r for r in out["radio_metrics"]}
    r = radios[("aa:bb:cc:00:00:03", "wifi1")]
    assert (r["band"], r["channel"], r["width_mhz"], r["util_total"], r["tx_power_dbm"]) == \
           ("5", 44, 80, 11, 23)

    ports = {(p["device_mac"], p["port_idx"]): p for p in out["port_metrics"]}
    assert ports[("aa:bb:cc:00:00:02", 5)]["poe_w"] == 7.42

    links = {(l["kind"], l["a_id"], l["b_id"]): l for l in out["links"]}
    up = links[("uplink", "aa:bb:cc:00:00:02", "aa:bb:cc:00:00:01")]
    assert (up["a_port"], up["b_port"], up["medium"], up["speed_mbps"]) == ("1", "2", "wired", 1000)
    assert links[("uplink", "aa:bb:cc:00:00:04", "aa:bb:cc:00:00:03")]["medium"] == "wireless"
    assert links[("lldp", "aa:bb:cc:00:00:02", sample.NAS)]["b_port"] == "eth0"
    assert not any(l["a_id"] == "aa:bb:cc:00:00:01" and l["kind"] == "uplink" for l in out["links"])


def test_clients():
    out = nz.clients(sample.ACTIVE, sample.KNOWN, "default", NOW)
    c = {r["mac"]: r for r in out["clients"]}
    phone = c["de:ad:be:ef:00:01"]
    assert phone["connected"] and phone["display_name"] == "Adam iPhone"
    assert phone["fixed_ip"] == "192.168.1.101" and phone["ap_mac"] == "aa:bb:cc:00:00:03"
    assert c["de:ad:be:ef:00:99"]["connected"] is False       # known but offline
    ws = c["de:ad:be:ef:00:04"]
    assert ws["is_wired"] and ws["sw_port"] == 3 and ws["ap_mac"] is None and ws["identity"] == "adam"

    m = {r["mac"]: r for r in out["client_metrics"]}
    assert len(m) == 4
    assert m["de:ad:be:ef:00:01"]["signal_dbm"] == -52 and m["de:ad:be:ef:00:01"]["band"] == "5"
    assert m["de:ad:be:ef:00:01"]["tx_bps"] == pytest.approx(10004.0)
    assert m["de:ad:be:ef:00:04"]["tx_bytes"] == 9000          # wired counters used
    assert {(l["a_id"], l["b_id"], l["b_port"]) for l in out["links"]} >= {
        ("de:ad:be:ef:00:04", "aa:bb:cc:00:00:02", "3")}


def test_wlans_networks_routes():
    w = nz.wlans(sample.WLANS, "default", NOW)
    assert "secret" not in json.dumps(w, default=str)
    assert w[0]["bands"] == "2g,5g" and w[0]["wpa3"] is True
    n = {r["name"]: r for r in nz.networks(sample.NETWORKS, "default", NOW)}
    assert n["IoT"]["subnet"] == "192.168.20.0/24" and n["IoT"]["gateway_ip"] == "192.168.20.1"
    assert n["WAN"]["subnet"] is None
    assert nz.gateway_mac(sample.DEVICES) == "aa:bb:cc:00:00:01"
    r = {x["prefix"]: x for x in nz.routes(sample.ROUTING, "gw", "default", NOW)}
    assert (r["0.0.0.0/0"]["protocol"], r["0.0.0.0/0"]["next_hop"], r["0.0.0.0/0"]["metric"]) == \
           ("static", "203.0.113.1", 0)
    assert r["192.168.1.0/24"]["protocol"] == "connected" and r["192.168.1.0/24"]["selected"]


def test_odd_input_does_not_crash():
    junk = [{}, {"mac": None}, {"mac": "AA:AA:AA:AA:AA:AA", "state": "weird", "uplink": "x",
                                "radio_table": [None, {}], "port_table": [{}, 3],
                                "lldp_table": [None, {"chassis_id": ""}], "uptime": "n/a"}]
    out = nz.devices(junk, "default", NOW)
    assert len(out["devices"]) == 1 and out["devices"][0]["uptime_s"] is None
    assert nz.clients([{}, {"mac": "bb:bb:bb:bb:bb:bb"}], [{}], "default", NOW)["clients"]
    assert nz.routes([{}, {"pfx": "10.0.0.0/8"}, {"pfx": "1.1.1.0/24", "nh": ["x"]}], "g", "s", NOW)


# ---------------------------------------------------------------- client behaviour
async def test_client_api_key_and_pagination():
    async with make_client() as c:
        assert len(await c.legacy("stat/device")) == 4
        assert await c.integration_site_id() == "site-uuid"
        assert len(await c.integration_all("sites/site-uuid/devices")) == 4   # 2 pages


async def test_client_session_fallback():
    async with make_client(key_ok_on_legacy=False, creds=True) as c:
        assert len(await c.legacy("stat/sta")) == 4
    async with make_client(key_ok_on_legacy=False) as c:
        with pytest.raises(UnifiError):
            await c.legacy("stat/sta")


async def test_collector_only_reads():
    client = make_client()
    await UnifiCollector(client).fetch()
    assert {m for m, _ in client._http._transport.state["requests"]} == {"GET"}
    await client.aclose()


async def test_integration_fallback_when_legacy_missing():
    async with make_client(legacy_ok=False) as c:
        snap = await UnifiCollector(c).fetch()
    assert snap["mode"] == "integration" and snap["warnings"]
    assert len(snap["devices"]) == 4 and len(snap["clients"]) == 4 and len(snap["links"]) == 4


# ---------------------------------------------------------------- database + API
@pytest.fixture
async def pool():
    p = await db.create_pool(DSN)
    await p.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    assert await db.migrate(p) == ["001_init.sql"]
    assert await db.migrate(p) == []            # idempotent
    yield p
    await p.close()


@needs_db
async def test_collect_store_and_api(pool, monkeypatch):
    client = make_client()
    col = UnifiCollector(client)
    counts, warnings = await col.run_once(pool)
    assert warnings == []
    assert counts == {"devices": 4, "clients": 5, "client_metrics": 4, "radio_metrics": 3,
                      "port_metrics": 4, "wlans": 2, "networks": 3, "routes": 4, "links": 10}

    # second poll: one client leaves; its last attachment must be remembered
    monkeypatch.setitem(sample.LEGACY, "stat/sta", sample.ACTIVE[1:])
    await col.run_once(pool)
    await client.aclose()
    row = await pool.fetchrow("SELECT * FROM clients WHERE mac = 'de:ad:be:ef:00:01'")
    assert row["connected"] is False and row["ap_mac"] == "aa:bb:cc:00:00:03"
    assert row["display_name"] == "Adam iPhone" and row["fixed_ip"] == "192.168.1.101"
    assert await pool.fetchval("SELECT count(*) FROM clients WHERE connected") == 3
    assert await pool.fetchval("SELECT count(*) FROM device_metrics") == 8
    assert "secret" not in await pool.fetchval("SELECT string_agg(raw::text, '') FROM wlans")

    from fastapi import FastAPI
    from nms.api.routes import router
    app = FastAPI()
    app.include_router(router)
    app.state.pool = pool
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as api:
        devs = (await api.get("/api/v1/devices")).json()
        assert len(devs) == 4 and devs[0]["name"] == "AP Garage"
        one = (await api.get("/api/v1/devices/AA:BB:CC:00:00:03")).json()
        assert len(one["radios"]) == 2
        assert len((await api.get("/api/v1/clients?connected=true")).json()) == 3
        assert len((await api.get("/api/v1/clients?search=thermo")).json()) == 1
        hist = (await api.get("/api/v1/clients/de:ad:be:ef:00:02")).json()
        assert len(hist["history"]) == 2
        assert len((await api.get("/api/v1/wireless/radios")).json()) == 3
        wl = {w["name"]: w for w in (await api.get("/api/v1/wireless/wlans")).json()}
        assert wl["IoT"]["clients"] == 2 and wl["IoT"]["vlan"] == 20
        assert len((await api.get("/api/v1/routes")).json()) == 4
        assert (await api.get("/api/v1/clients/00:00:00:00:00:00")).status_code == 404

        topo = (await api.get("/api/v1/topology")).json()
        kinds = {n["id"]: n["kind"] for n in topo["nodes"]}
        assert kinds["aa:bb:cc:00:00:01"] == "gateway" and kinds[sample.NAS] == "neighbor"
        assert kinds["net:192.168.20.0/24"] == "network" and "client" not in kinds.values()
        pairs = {frozenset((e["source"], e["target"])): e for e in topo["edges"]}
        # gateway<->switch is reported three times (uplink + LLDP from each end): one edge
        gs = pairs[frozenset(("aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02"))]
        assert gs["kind"] == "uplink" and len(topo["edges"]) == len(pairs) == 8
        iot = next(n for n in topo["nodes"] if n["id"] == "net:192.168.20.0/24")
        assert "IoT" in iot["subtitle"]

        full = (await api.get("/api/v1/topology?include_clients=true&include_routes=false")).json()
        assert sum(n["kind"] == "client" for n in full["nodes"]) == 3
        assert not any(n["kind"] == "network" for n in full["nodes"])
        assert len(full["edges"]) == 7

        monkeypatch.setenv("NMS_API_TOKEN", "s3cret")
        from nms.config import get_settings
        get_settings.cache_clear()
        assert (await api.get("/api/v1/devices")).status_code == 401
        ok = await api.get("/api/v1/devices", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200
        get_settings.cache_clear()
