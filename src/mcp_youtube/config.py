"""Validated, env-driven configuration for mcp-youtube.

Loads values from environment variables (and a ``.env`` file when present),
validates types/ranges, and prepares optional Webshare proxy support without
wiring it in v0.1 (env vars are accepted but proxy is not yet plumbed).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the MCP YouTube server.

    All fields can be overridden via environment variables. Names map 1:1 with
    the env var names (case-insensitive). Pydantic validates them at startup.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # Anti-ban / rate limiting
    # ------------------------------------------------------------------
    rate_limit_min_seconds: float = Field(
        default=5.0,
        ge=0.0,
        le=60.0,
        description="Minimum seconds between fetch_transcript calls.",
    )
    rate_limit_max_seconds: float = Field(
        default=10.0,
        ge=0.0,
        le=120.0,
        description="Maximum seconds between fetch_transcript calls (random jitter).",
    )

    # ------------------------------------------------------------------
    # Default fetch behaviour
    # ------------------------------------------------------------------
    default_language: str = Field(
        default="en",
        description="Default preferred language code (ISO-639-1).",
    )
    fallback_languages: str = Field(
        default="en-US,en-GB",
        description=(
            "Comma-separated fallback language codes appended after the "
            "primary language preference."
        ),
    )

    # ------------------------------------------------------------------
    # Webshare proxy (reserved — NOT wired in v0.1)
    # ------------------------------------------------------------------
    webshare_proxy_username: str = Field(default="")
    webshare_proxy_password: str = Field(default="")

    # ------------------------------------------------------------------
    # MCP server settings
    # ------------------------------------------------------------------
    mcp_host: str = Field(default="0.0.0.0")
    mcp_port: int = Field(default=3716, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(default="INFO")
    log_format: Literal["json", "text"] = Field(default="json")

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @model_validator(mode="after")
    def _check_rate_limit_window(self) -> Settings:
        if self.rate_limit_max_seconds < self.rate_limit_min_seconds:
            raise ValueError("RATE_LIMIT_MAX_SECONDS must be >= RATE_LIMIT_MIN_SECONDS")
        return self

    def fallback_language_list(self) -> list[str]:
        """Return the fallback language list as a clean list of codes."""
        return [c.strip() for c in self.fallback_languages.split(",") if c.strip()]

    def safe_repr(self) -> dict[str, object]:
        """Return a redacted dict suitable for logging at startup."""
        return {
            "rate_limit_min_seconds": self.rate_limit_min_seconds,
            "rate_limit_max_seconds": self.rate_limit_max_seconds,
            "default_language": self.default_language,
            "fallback_languages": self.fallback_languages,
            "webshare_proxy_configured": bool(
                self.webshare_proxy_username and self.webshare_proxy_password
            ),
            "mcp_host": self.mcp_host,
            "mcp_port": self.mcp_port,
            "log_level": self.log_level,
            "log_format": self.log_format,
        }


def load_settings() -> Settings:
    """Build a Settings instance from the environment. Raises on invalid config."""
    return Settings()
