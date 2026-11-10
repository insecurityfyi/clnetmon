"""HTTP client for a UniFi OS console (UDM / UCG / Cloud Key Gen2+ ...).

Two API surfaces are used:

* Integration API  /proxy/network/integration/v1/...   official, X-API-KEY, paginated,
  but only a light set of fields.
* Legacy API       /proxy/network/api/s/{site}/...      the detailed data the UniFi UI
  itself uses (RSSI, rates, radio stats, LLDP, routing). Recent consoles accept the
  same X-API-KEY here; older ones need a session login, which is used as a fallback
  when a username/password is configured.

Read-only: this client only ever issues GETs (plus the login POST).
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


class UnifiError(Exception):
    pass


class UnifiClient:
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        site: str = "default",
        verify_tls: bool = False,
        username: str = "",
        password: str = "",
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.site = site
        self._api_key = api_key
        self._username = username
        self._password = password
        self._logged_in = False
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            verify=verify_tls,
            timeout=timeout,
            headers={"Accept": "application/json"},
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "UnifiClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ low level
    async def _login(self) -> None:
        if not (self._username and self._password):
            raise UnifiError("API key rejected and no username/password configured for fallback")
        r = await self._http.post(
            "/api/auth/login",
            json={"username": self._username, "password": self._password},
        )
        if r.status_code != 200:
            raise UnifiError(f"session login failed: HTTP {r.status_code}")
        self._logged_in = True
        log.info("unifi: using session login for legacy endpoints")

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        headers = {"X-API-KEY": self._api_key} if self._api_key and not self._logged_in else {}
        try:
            r = await self._http.get(path, params=params, headers=headers)
            if r.status_code in (401, 403) and not self._logged_in and self._username:
                await self._login()
                r = await self._http.get(path, params=params)
            elif r.status_code == 401 and self._logged_in:
                self._logged_in = False  # session expired; log in again once
                await self._login()
                r = await self._http.get(path, params=params)
        except httpx.HTTPError as e:
            raise UnifiError(f"GET {path}: {type(e).__name__}: {e}") from e
        if r.status_code != 200:
            raise UnifiError(f"GET {path}: HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as e:
            raise UnifiError(f"GET {path}: response was not JSON") from e

    # ------------------------------------------------------------------ legacy API
    async def legacy(self, path: str) -> list[dict[str, Any]]:
        """GET a site-scoped legacy endpoint, e.g. legacy('stat/sta')."""
        body = await self._get(f"/proxy/network/api/s/{self.site}/{path.lstrip('/')}")
        if isinstance(body, dict):
            meta = body.get("meta") or {}
            if meta.get("rc") not in (None, "ok"):
                raise UnifiError(f"{path}: {meta.get('msg', 'controller returned an error')}")
            data = body.get("data", [])
            return data if isinstance(data, list) else [data]
        return body if isinstance(body, list) else []

    # ------------------------------------------------------------------ integration API
    async def integration(self, path: str) -> Any:
        return await self._get(f"/proxy/network/integration/v1/{path.lstrip('/')}")

    async def integration_all(self, path: str, limit: int = 200) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = await self._get(
                f"/proxy/network/integration/v1/{path.lstrip('/')}",
                params={"offset": offset, "limit": limit},
            )
            data = page.get("data", [])
            out.extend(data)
            offset += len(data)
            if not data or offset >= page.get("totalCount", 0):
                return out

    async def integration_site_id(self) -> str | None:
        """UUID of the configured site, as the integration API identifies it."""
        sites = await self.integration_all("sites")
        for s in sites:
            if s.get("internalReference") == self.site:
                return s.get("id")
        return sites[0].get("id") if sites else None
