"""Runtime settings, all from environment variables (or backend/.env)."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "backend/.env"), extra="ignore")

    env: str = "development"
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/pm5"
    redis_url: str = "redis://localhost:6379/0"

    # Encrypts wallet secrets at rest (Fernet). Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    master_key: str = Field(default="", description="Fernet key for encrypting wallet secrets")
    # Signs dashboard session tokens.
    jwt_secret: str = Field(default="", description="random string, 32+ chars")
    jwt_ttl_hours: int = 24 * 7
    # First-run admin password; hashed into the users table on first boot, then ignored.
    admin_password: str = ""

    # comma-separated (plain string so it survives shell `source .env` and docker env_file alike)
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    # Trading safety. Live orders need BOTH this env flag AND the in-app live switch.
    allow_live_trading: bool = False

    # Optional fallback wallet from env (used only if no wallet is saved in the app)
    private_key: str = ""
    funder_address: str = ""
    signature_type: int = 1

    # Market-data pipeline (Binance + Polymarket collectors). Off in tests.
    market_data_enabled: bool = True
    # Weather data collection (read only, no orders): METAR reports, ensemble forecasts and price ladders
    weather_enabled: bool = True
    weather_cities: str = "london,nyc,hong-kong,munich,madrid,paris,chicago,tokyo"
    price_history_retention_days: int = 30
    snapshot_retention_days: int = 90

    # External endpoints
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    data_api_host: str = "https://data-api.polymarket.com"
    clob_ws: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    binance_rest: str = "https://data-api.binance.vision"
    binance_ws: str = "wss://data-stream.binance.vision/stream"

    def validate_for_runtime(self) -> list[str]:
        problems = []
        if not self.master_key:
            problems.append("MASTER_KEY is not set (needed to encrypt the wallet key)")
        if len(self.jwt_secret) < 32:
            problems.append("JWT_SECRET must be at least 32 characters")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
