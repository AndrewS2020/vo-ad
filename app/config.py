from __future__ import annotations
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

BASE_DIR = Path(__file__).resolve().parent.parent


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

    # App Settings
    temp_audio_dir: Path = Field(default=BASE_DIR / "temp_audio", alias="TEMP_AUDIO_DIR")
    # The city the address registry (data/addr.csv) covers. Swapping cities
    # means swapping the registry data AND this value — the registry itself
    # decides which streets exist, this just tells Gemini what to expect.
    city: str = Field(default="Київ", alias="CITY")

    def init_dirs(self) -> None:
        """Creates required directories (e.g. temp_audio)."""
        self.temp_audio_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.init_dirs()
