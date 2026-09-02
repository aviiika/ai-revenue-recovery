"""Application settings, loaded from environment only.

Spec (NFR Security): no secrets in source, server-side credentials only, and an
explicit ``RAZORPAY_MODE=test`` safety gate.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["local", "ci", "demo"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    app_env: AppEnv = "local"
    log_level: str = "INFO"
    api_port: int = 8000

    database_url: str = "postgresql+psycopg://revrec:revrec@localhost:5432/revrec"
    test_database_url: str = "postgresql+psycopg://revrec:revrec@localhost:5432/revrec_test"

    redis_url: str = "redis://localhost:6379/0"

    # Razorpay is not called until Milestone 5; the gate is defined now so the
    # invariant cannot be forgotten later.
    razorpay_mode: str = "test"
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    llm_provider: str = "none"
    llm_model: str = ""
    llm_api_key: str = ""

    demo_endpoints_enabled: bool = False
    synthetic_seed: int = Field(default=20260902, ge=0)

    @field_validator("razorpay_mode")
    @classmethod
    def _enforce_test_mode(cls, value: str) -> str:
        """Refuse to boot against Razorpay live mode.

        The buildathon rules and the spec both forbid live keys. Failing at
        startup is far safer than discovering it at the first API call.
        """
        normalised = value.strip().lower()
        if normalised != "test":
            raise ValueError(
                f"RAZORPAY_MODE must be 'test' (got {value!r}). "
                "This project is not permitted to run against live Razorpay credentials."
            )
        return normalised

    @property
    def llm_enabled(self) -> bool:
        """Whether an LLM is configured.

        When false the system must still work: explanations fall back to
        deterministic templates (spec: "the demo survives LLM degradation").
        """
        return self.llm_provider.lower() != "none" and bool(self.llm_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
