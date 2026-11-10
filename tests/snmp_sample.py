"""Made-up SNMP agents: a third-party switch, a firewall, a printer, and the UniFi core
switch from sample.py answering SNMP as well. Each agent is {oid: (snmprec-tag, value)}.

FakeSession serves them in-process; write_snmprec() dumps the same data for snmpsim so
the real pysnmp client can be exercised over UDP.
"""
from __future__ import annotations

from nms.integrations.snmp.client import SnmpError

INT, STR, OID, IP, C32, GAUGE, TICKS, C64 = "2", "4x", "6", "64", "65", "66", "67", "70"


def m(s: str) -> bytes:
    return bytes(int(x, 16) for x in s.split(":"))


def mi(s: str) -> str:  # MAC as OID index
    return ".".join(str(int(x, 16)) for x in s.split(":"))


def system(name: str, descr: str, uptime: int = 123400) -> dict:
    return {"1.3.6.1.2.1.1.1.0": (STR, descr.encode()),
            "1.3.6.1.2.1.1.2.0": (OID, "1.3.6.1.4.1.99999.1"),
            "1.3.6.1.2.1.1.3.0": (TICKS, uptime),
            "1.3.6.1.2.1.1.4.0": (STR, b"noc"),
            "1.3.6.1.2.1.1.5.0": (STR, name.encode()),
            "1.3.6.1.2.1.1.6.0": (STR, b"rack 1")}


def iface(i: int, name: str, mac_: str | None, up: bool = True, speed_mb: int = 1000,
          rx: int = 1000, tx: int = 2000, alias: str = "", iftype: int = 6) -> dict:
    t, x = "1.3.6.1.2.1.2.2.1", "1.3.6.1.2.1.31.1.1.1"
    d = {f"{t}.2.{i}": (STR, name.encode()), f"{t}.3.{i}": (INT, iftype),
         f"{t}.5.{i}": (GAUGE, min(speed_mb * 1_000_000, 4294967295)),
         f"{t}.6.{i}": (STR, m(mac_) if mac_ else b""),
         f"{t}.7.{i}": (INT, 1), f"{t}.8.{i}": (INT, 1 if up else 2),
         f"{t}.13.{i}": (C32, 0), f"{t}.14.{i}": (C32, 3), f"{t}.19.{i}": (C32, 0),
         f"{t}.20.{i}": (C32, 1),
         f"{x}.1.{i}": (STR, name.encode()), f"{x}.6.{i}": (C64, rx), f"{x}.10.{i}": (C64, tx),
         f"{x}.15.{i}": (GAUGE, speed_mb), f"{x}.18.{i}": (STR, alias.encode())}
    return d


UNIFI_SW = "aa:bb:cc:00:00:02"        # "Core Switch" in sample.py
EDGE_SW, FW, PRN = "00:1b:54:00:00:10", "00:0d:b9:00:00:20", "00:17:c8:00:00:30"
PC = "de:ad:be:ef:00:04"              # "workstation" client in sample.py

# --- third-party access switch: LLDP to the UniFi core switch and to the firewall
EDGE = {
    **system("edge-sw1", "ExampleNet 2930 Switch, revision 16.10"),
    "1.3.6.1.2.1.17.1.1.0": (STR, m(EDGE_SW)),
    "1.0.8802.1.1.2.1.3.1.0": (INT, 4), "1.0.8802.1.1.2.1.3.2.0": (STR, m(EDGE_SW)),
    **iface(1, "1/1", EDGE_SW, alias="uplink to core"), **iface(2, "1/2", "00:1b:54:00:00:12"),
    **iface(3, "1/3", "00:1b:54:00:00:13", up=False), **iface(99, "vlan1", EDGE_SW, iftype=53),
    # lldpRemTable index: timeMark.localPort.remIndex
    "1.0.8802.1.1.2.1.4.1.1.4.0.1.1": (INT, 4), "1.0.8802.1.1.2.1.4.1.1.5.0.1.1": (STR, m(UNIFI_SW)),
    "1.0.8802.1.1.2.1.4.1.1.6.0.1.1": (INT, 5), "1.0.8802.1.1.2.1.4.1.1.7.0.1.1": (STR, b"Port 9"),
    "1.0.8802.1.1.2.1.4.1.1.9.0.1.1": (STR, b"Core Switch"),
    "1.0.8802.1.1.2.1.4.1.1.4.0.2.1": (INT, 7), "1.0.8802.1.1.2.1.4.1.1.5.0.2.1": (STR, b"fw-local-id"),
    "1.0.8802.1.1.2.1.4.1.1.6.0.2.1": (INT, 7), "1.0.8802.1.1.2.1.4.1.1.7.0.2.1": (STR, b"igb1"),
    "1.0.8802.1.1.2.1.4.1.1.9.0.2.1": (STR, b"fw1"),
    # bridge port -> ifIndex, and per-VLAN MAC table (fdbId.mac -> bridge port)
    "1.3.6.1.2.1.17.1.4.1.2.1": (INT, 1), "1.3.6.1.2.1.17.1.4.1.2.2": (INT, 2),
    "1.3.6.1.2.1.17.1.4.1.2.3": (INT, 3),
    f"1.3.6.1.2.1.17.7.1.2.2.1.2.1.{mi(UNIFI_SW)}": (INT, 1),
    f"1.3.6.1.2.1.17.7.1.2.2.1.2.1.{mi(PC)}": (INT, 1),
    f"1.3.6.1.2.1.17.7.1.2.2.1.2.1.{mi(FW)}": (INT, 2),
    f"1.3.6.1.2.1.17.7.1.2.2.1.2.20.{mi(PRN)}": (INT, 3),
}

# --- firewall: routes (inetCidrRouteTable), ARP, CPU and memory
_R = "1.3.6.1.2.1.4.24.7.1"


def _inet_route(dest: str, plen: int, nh: str | None, ifindex: int, proto: int) -> dict:
    idx = f"1.4.{dest}.{plen}.2.0.0." + (f"1.4.{nh}" if nh else "0.0")
    return {f"{_R}.7.{idx}": (INT, ifindex), f"{_R}.9.{idx}": (INT, proto),
            f"{_R}.12.{idx}": (INT, 0)}


FIREWALL = {
    **system("fw1", "pfSense fw1.home.arpa 2.7.2-RELEASE FreeBSD amd64"),
    "1.3.6.1.2.1.4.1.0": (INT, 1),
    **iface(1, "igb0", FW, alias="WAN"), **iface(2, "igb1", "00:0d:b9:00:00:21", alias="LAN"),
    **_inet_route("0.0.0.0", 0, "198.51.100.1", 1, 3),
    **_inet_route("192.168.7.0", 24, None, 2, 2),
    **_inet_route("10.99.0.0", 16, "192.168.7.254", 2, 13),
    "1.3.6.1.2.1.4.22.1.2.2.192.168.7.50": (STR, m(PC)),
    "1.3.6.1.2.1.4.22.1.2.2.192.168.7.60": (STR, m(PRN)),
    "1.3.6.1.2.1.25.3.3.1.2.1": (INT, 10), "1.3.6.1.2.1.25.3.3.1.2.2": (INT, 30),
    "1.3.6.1.2.1.25.2.3.1.2.1": (OID, "1.3.6.1.2.1.25.2.1.2"),
    "1.3.6.1.2.1.25.2.3.1.5.1": (INT, 4000), "1.3.6.1.2.1.25.2.3.1.6.1": (INT, 1000),
    "1.3.6.1.2.1.25.2.3.1.2.2": (OID, "1.3.6.1.2.1.25.2.1.4"),
    "1.3.6.1.2.1.25.2.3.1.5.2": (INT, 9000), "1.3.6.1.2.1.25.2.3.1.6.2": (INT, 8000),
}

# --- printer
PRINTER = {
    **system("office-printer", "Example LaserJet Pro; firmware 2024.1"),
    **iface(1, "eth0", PRN, speed_mb=100),
    "1.3.6.1.2.1.25.3.2.1.3.1": (STR, b"Example LaserJet Pro M404"),
    "1.3.6.1.2.1.25.3.5.1.1.1": (INT, 3),
    "1.3.6.1.2.1.43.10.2.1.4.1.1": (C32, 48211),
    "1.3.6.1.2.1.43.11.1.1.6.1.1": (STR, b"Black Cartridge"),
    "1.3.6.1.2.1.43.11.1.1.8.1.1": (INT, 100), "1.3.6.1.2.1.43.11.1.1.9.1.1": (INT, 37),
    "1.3.6.1.2.1.43.11.1.1.6.1.2": (STR, b"Imaging Drum"),
    "1.3.6.1.2.1.43.11.1.1.8.1.2": (INT, -2), "1.3.6.1.2.1.43.11.1.1.9.1.2": (INT, -3),
    "1.3.6.1.2.1.43.16.5.1.2.1.1": (STR, b"Ready"),
}

# --- the UniFi core switch answering SNMP too: must merge with the UniFi device
UNIFI_SWITCH = {
    **system("Core-Switch", "US-24-250W, 7.1.26, Linux 3.6.5"),
    **iface(1, "Port 1", "aa:bb:cc:00:00:99"), **iface(2, "Port 2", UNIFI_SW),
    # only the old ipRouteTable and the old (non-VLAN) FDB
    "1.3.6.1.2.1.4.21.1.2.192.168.7.0": (INT, 2), "1.3.6.1.2.1.4.21.1.7.192.168.7.0": (IP, "0.0.0.0"),
    "1.3.6.1.2.1.4.21.1.9.192.168.7.0": (INT, 2),
    "1.3.6.1.2.1.4.21.1.11.192.168.7.0": (IP, "255.255.255.0"),
    f"1.3.6.1.2.1.17.4.3.1.2.{mi(EDGE_SW)}": (INT, 2),
}

AGENTS = {"192.168.7.2": UNIFI_SWITCH, "192.168.7.10": EDGE, "192.168.7.1": FIREWALL,
          "192.168.7.60": PRINTER,
          "192.168.7.254": FIREWALL}   # second address of the same firewall
COMMUNITY = "s3cr3t-ro"


def _key(oid: str) -> tuple[int, ...]:
    return tuple(int(x) for x in oid.split("."))


class FakeSession:
    def __init__(self, host: str, community: str, timeout: float = 2.0, retries: int = 1,
                 agents: dict | None = None) -> None:
        agents = AGENTS if agents is None else agents
        self.host = host
        self._data = agents.get(host) if community == COMMUNITY else None
        self._sorted = sorted((_key(o), v[1]) for o, v in (self._data or {}).items())

    async def get(self, *oids: str):
        if self._data is None:
            raise SnmpError(f"{self.host}: No SNMP response received before timeout")
        return {o: self._data[o][1] for o in oids if o in self._data}

    async def walk(self, base: str, limit: int = 20000):
        if self._data is None:
            raise SnmpError(f"{self.host}: No SNMP response received before timeout")
        root = _key(base)
        return [(k[len(root):], v) for k, v in self._sorted if k[:len(root)] == root][:limit]


def write_snmprec(agent: dict, path: str) -> None:
    lines = []
    for oid in sorted(agent, key=_key):
        tag, val = agent[oid]
        if tag == STR:
            val = val.hex()
        lines.append(f"{oid}|{tag}|{val}")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
