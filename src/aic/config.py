"""Typed application configuration.

Every knob is validated at startup, so a bad value fails the process rather than
the first request that happens to touch it.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from aic.domain.models import RiskLevel

REPO_ROOT = Path(__file__).resolve().parents[2]


class LLMProvider(StrEnum):
    ANTHROPIC = "anthropic"
    #: Deterministic scripted model. Lets the whole system run with no credentials.
    FAKE = "fake"


class LogFormat(StrEnum):
    JSON = "json"
    CONSOLE = "console"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AIC_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = "local"
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.JSON

    # -- LLM --------------------------------------------------------------
    llm_provider: LLMProvider = LLMProvider.FAKE
    llm_model: str = "claude-opus-5"
    llm_effort: str = "high"
    llm_max_tokens: int = Field(default=16_000, ge=1024, le=128_000)
    llm_timeout_seconds: float = Field(default=120.0, gt=0)
    #: Read from ANTHROPIC_API_KEY (no AIC_ prefix) to match the SDK's convention.
    anthropic_api_key: SecretStr | None = Field(default=None, alias="ANTHROPIC_API_KEY")

    # -- Persistence ------------------------------------------------------
    database_url: str = "postgresql://aic:aic@localhost:5432/aic"
    redis_url: str = "redis://localhost:6379/0"

    # -- Guardrails -------------------------------------------------------
    auto_approve_max_risk: RiskLevel = RiskLevel.READ_ONLY
    max_agent_retries: int = Field(default=2, ge=0, le=5)
    investigation_timeout_seconds: float = Field(default=300.0, gt=0)

    # -- Corpus -----------------------------------------------------------
    runbook_directory: Path = REPO_ROOT / "docs" / "runbooks"
    embedding_dimensions: int = Field(default=512, ge=64, le=4096)

    @field_validator("llm_effort")
    @classmethod
    def _known_effort(cls, value: str) -> str:
        allowed = {"low", "medium", "high", "xhigh", "max"}
        if value not in allowed:
            raise ValueError(f"llm_effort must be one of {sorted(allowed)}, got {value!r}")
        return value

    @field_validator("auto_approve_max_risk")
    @classmethod
    def _never_auto_approve_high(cls, value: RiskLevel) -> RiskLevel:
        if value is RiskLevel.HIGH:
            raise ValueError(
                "auto_approve_max_risk may not be 'high' — destructive actions "
                "always require a human decision"
            )
        return value

    @property
    def use_real_llm(self) -> bool:
        return self.llm_provider is LLMProvider.ANTHROPIC


def load_settings(**overrides: object) -> Settings:
    """Build settings from the environment, with optional test overrides."""
    return Settings(**overrides)  # type: ignore[arg-type]
