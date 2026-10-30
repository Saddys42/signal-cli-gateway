from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SG_", env_file=".env", extra="ignore")

    # --- signal-cli daemon (JSON-RPC over TCP) ---
    signal_cli_host: str = "127.0.0.1"
    signal_cli_port: int = 7583
    rpc_timeout: float = 600.0  # big accounts: first listGroups can take minutes
    # Local signal-cli data (read-only group listing straight from the
    # synced database, for accounts too big for a timely listGroups RPC).
    signal_cli_data_dir: str = "signal-cli-data"

    # --- HTTP API ---
    host: str = "0.0.0.0"
    port: int = 8000
    # If set, every /v1/* call must send header:  X-API-Key: <api_key>
    # Leave empty ONLY for local development.
    api_key: str = ""

    # --- Outbound events ---
    # If set, every incoming event is also POSTed here as JSON.
    webhook_url: str = ""
    webhook_secret: str = ""  # sent as X-SG-Signature: sha256=<hmac>

    # --- Anti-abuse / rate limiting (per account) ---
    # Signal bans numbers that behave like bulk senders. Keep this conservative:
    # sends from the same account are serialised with a minimum gap.
    send_min_interval_seconds: float = 1.0
    send_burst_per_minute: int = 20


@lru_cache
def get_settings() -> Settings:
    return Settings()
