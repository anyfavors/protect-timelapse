"""
Single source of truth for all environment variable reads.
No other file may call os.getenv() or access os.environ directly.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # UniFi Protect NVR
    protect_host: str = "argos.local"
    protect_port: int = 443
    protect_username: str = ""
    protect_password: str = ""
    protect_verify_ssl: bool = False

    # Storage paths
    database_path: str = "/data/timelapse.db"
    frames_path: str = "/data/frames"
    thumbnails_path: str = "/data/thumbs"
    renders_path: str = "/data/renders"

    # Geolocation (Helsingør defaults) — used by astral for sunrise/sunset
    latitude: float = 56.0361
    longitude: float = 12.6136

    # Timezone — used for UI display and astral calculations
    tz: str = "Europe/Copenhagen"

    # Logging
    log_level: str = "INFO"

    # FFmpeg
    ffmpeg_threads: int = 4
    ffmpeg_timeout_seconds: int = 7200  # 2 hours

    # Database connection pool size
    db_pool_size: int = 4

    # Optional API key — if set, all endpoints require X-Api-Key header (S1)
    api_key: str = ""


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the cached Settings instance (loaded once at startup)."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
