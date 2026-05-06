"""Centralized configuration via pydantic-settings."""
from typing import Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8",
        case_sensitive=False, extra="ignore",
    )

    bot_token: str = Field(default="YOUR_TOKEN_HERE", alias="BOT_TOKEN")

    whale_alert_api_key: Optional[str] = Field(default=None, alias="WHALE_ALERT_API_KEY")
    massive_api_key: Optional[str] = Field(default=None, alias="MASSIVE_API_KEY")
    twelvedata_api_key: Optional[str] = Field(default=None, alias="TWELVEDATA_API_KEY")
    coinmarketcap_api_key: Optional[str] = Field(default=None, alias="COINMARKETCAP_API_KEY")
    etherscan_api_key: Optional[str] = Field(default=None, alias="ETHERSCAN_API_KEY")

    anthropic_api_key: Optional[str] = Field(default=None, alias="ANTHROPIC_API_KEY")
    gemini_api_key: Optional[str] = Field(default=None, alias="GEMINI_API_KEY")
    openai_api_key: Optional[str] = Field(default=None, alias="OPENAI_API_KEY")

    scan_top_n: int = Field(default=100, alias="SCAN_TOP_N")
    min_volume_usd: float = Field(default=500_000, alias="MIN_VOLUME_USD")
    min_confidence: int = Field(default=65, alias="MIN_CONFIDENCE")
    min_sources_agree: int = Field(default=4, alias="MIN_SOURCES_AGREE")
    alert_cooldown: int = Field(default=3600, alias="ALERT_COOLDOWN")
    scan_interval: int = Field(default=180, alias="SCAN_INTERVAL")
    monitor_interval: int = Field(default=60, alias="MONITOR_INTERVAL")
    default_mode: str = Field(default="day", alias="DEFAULT_MODE")

    db_path: str = Field(default="/tmp/explosion_bot.db", alias="DB_PATH")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    binance_rest: str = "https://fapi.binance.com"
    excluded_pairs: set[str] = {
        "USDTUSDT", "BUSDUSDT", "USDCUSDT", "DAIUSDT", "TUSDUSDT",
        "FDUSDUSDT", "WBTCUSDT", "WETHUSDT", "STETHUSDT",
    }

    source_weights: dict = {
        "volume_delta": 0.22, "order_book": 0.18, "whale_flow": 0.18,
        "funding_divergence": 0.14, "btc_trend": 0.10,
        "multi_timeframe": 0.10, "btc_correlation": 0.08,
    }

    @property
    def has_whale_alert(self) -> bool: return bool(self.whale_alert_api_key)
    @property
    def has_massive(self) -> bool: return bool(self.massive_api_key)
    @property
    def has_twelvedata(self) -> bool: return bool(self.twelvedata_api_key)
    @property
    def has_cmc(self) -> bool: return bool(self.coinmarketcap_api_key)
    @property
    def has_ai(self) -> bool:
        return any([self.anthropic_api_key, self.gemini_api_key, self.openai_api_key])


settings = Settings()
