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

    retention_days: int = 90

    @property
    def unifi_enabled(self) -> bool:
        return bool(self.unifi_url and (self.unifi_api_key or self.unifi_username))


@lru_cache
def get_settings() -> Settings:
    return Settings()
