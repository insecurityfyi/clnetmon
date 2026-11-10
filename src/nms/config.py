from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NMS_", env_file=".env", extra="ignore")

    database_url: str = "postgresql:///nms?host=/var/run/postgresql"

    api_host: str = "127.0.0.1"
    api_port: int = 8080
    api_token: str = ""

    unifi_url: str = ""
    unifi_api_key: str = ""
    unifi_site: str = "default"
    unifi_verify_tls: bool = False
    unifi_username: str = ""
    unifi_password: str = ""
    unifi_interval: int = 60

    # SNMP (v2c). Lists are comma- or space-separated.
    snmp_communities: str = ""        # tried in order; first that answers is remembered
    snmp_targets: str = ""            # hosts/IPs always polled
    snmp_discovery: str = ""          # CIDRs to sweep, and/or "auto" (subnets learned from UniFi)
    snmp_interval: int = 120
    snmp_discovery_interval: int = 3600
    snmp_timeout: float = 2.0

    retention_days: int = 90

    @property
    def unifi_enabled(self) -> bool:
        return bool(self.unifi_url and (self.unifi_api_key or self.unifi_username))

    @staticmethod
    def _list(v: str) -> list[str]:
        return [x for x in v.replace(",", " ").split() if x]

    @property
    def snmp_community_list(self) -> list[str]:
        return self._list(self.snmp_communities)

    @property
    def snmp_enabled(self) -> bool:
        return bool(self.snmp_community_list
                    and (self._list(self.snmp_targets) or self._list(self.snmp_discovery)))


@lru_cache
def get_settings() -> Settings:
    return Settings()
