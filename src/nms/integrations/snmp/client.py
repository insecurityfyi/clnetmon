"""Minimal async SNMPv2c reader (GET and bulk walk) on top of pysnmp. Read-only."""
from __future__ import annotations

from typing import Any

from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData, ContextData, ObjectIdentity, ObjectType, SnmpEngine, UdpTransportTarget,
    bulk_walk_cmd, get_cmd,
)
from pyasn1.type import univ
from pysnmp.proto import rfc1902, rfc1905

Index = tuple[int, ...]


class SnmpError(Exception):
    pass


def _value(v: Any) -> Any:
    """pysnmp value -> int | bytes | str, or None for 'no such object' markers."""
    if isinstance(v, (rfc1905.NoSuchObject, rfc1905.NoSuchInstance, rfc1905.EndOfMibView)):
        return None
    if isinstance(v, rfc1902.IpAddress):
        return v.prettyPrint()
    if isinstance(v, univ.ObjectIdentifier) or v.__class__.__name__ == "ObjectIdentity":
        return ".".join(str(x) for x in v)
    if isinstance(v, univ.OctetString):
        return bytes(v)
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _oid(s: str) -> Index:
    return tuple(int(x) for x in s.strip(".").split("."))


class SnmpSession:
    """One device + community. Engine is shared between sessions."""

    def __init__(self, engine: SnmpEngine, host: str, community: str, port: int = 161,
                 timeout: float = 2.0, retries: int = 1) -> None:
        self.host = host
        self._engine = engine
        self._auth = CommunityData(community, mpModel=1)  # v2c
        self._addr = (host, port)
        self._timeout, self._retries = timeout, retries
        self._target: UdpTransportTarget | None = None

    async def _tgt(self) -> UdpTransportTarget:
        if self._target is None:
            try:
                self._target = await UdpTransportTarget.create(
                    self._addr, timeout=self._timeout, retries=self._retries)
            except Exception as e:  # DNS failure, bad address
                raise SnmpError(f"{self.host}: {e}") from e
        return self._target

    async def get(self, *oids: str) -> dict[str, Any]:
        """GET scalars. OIDs the device does not have are simply absent from the result."""
        err_ind, err_stat, _, binds = await get_cmd(
            self._engine, self._auth, await self._tgt(), ContextData(),
            *[ObjectType(ObjectIdentity(o)) for o in oids])
        if err_ind:
            raise SnmpError(f"{self.host}: {err_ind}")
        if err_stat:
            raise SnmpError(f"{self.host}: {err_stat.prettyPrint()}")
        out = {}
        for name, val in binds:
            v = _value(val)
            if v is not None:
                out[".".join(str(x) for x in name)] = v
        return out

    async def walk(self, base: str, limit: int = 20000) -> list[tuple[Index, Any]]:
        """Bulk-walk one subtree. Returns (index-after-base, value) pairs; empty if the
        device does not implement it."""
        root = _oid(base)
        out: list[tuple[Index, Any]] = []
        async for err_ind, err_stat, _, binds in bulk_walk_cmd(
                self._engine, self._auth, await self._tgt(), ContextData(), 0, 25,
                ObjectType(ObjectIdentity(base)), lexicographicMode=False):
            if err_ind:
                if out:
                    break  # partial table is still useful
                raise SnmpError(f"{self.host}: {err_ind}")
            if err_stat:
                break
            for name, val in binds:
                oid = tuple(int(x) for x in name)
                if oid[:len(root)] != root:
                    return out
                v = _value(val)
                if v is not None:
                    out.append((oid[len(root):], v))
            if len(out) >= limit:
                break
        return out
