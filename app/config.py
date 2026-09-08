from __future__ import annotations
from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # Telegram Bot
    telegram_bot_token: str = Field(..., alias="TELEGRAM_BOT_TOKEN")

    # Google Gemini
    gemini_api_key: str = Field(..., alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-3.5-transcribe-live", alias="GEMINI_MODEL")

    # Validation / Geocoder Settings
    # Варианты: 'nominatim' (OpenStreetMap) или 'google' (Google Address Validation API)
    geocoder_provider: str = Field(default="nominatim", alias="GEOCODER_PROVIDER")
    google_maps_api_key: Optional[str] = Field(default=None, alias="GOOGLE_MAPS_API_KEY")

    # App Settings
    default_city: str = Field(default="Київ", alias="DEFAULT_CITY")
    search_old_street_names: bool = Field(default=True, alias="SEARCH_OLD_STREET_NAMES")
    temp_audio_dir: Path = Field(default=Path("temp_audio"), alias="TEMP_AUDIO_DIR")

    @property
    def effective_google_maps_key(self) -> str:
        """Returns a dedicated maps key, or falls back to the Gemini API key."""
        return self.google_maps_api_key or self.gemini_api_key

    def init_dirs(self) -> None:
        """Creates required directories (e.g. temp_audio)."""
        self.temp_audio_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.init_dirs()
