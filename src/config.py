"""Environment-driven configuration for the underwriting agent."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    companies_house_api_key: str
    # Postgres holding the loaded Companies House bulk data. Required: name
    # resolution reads from it, and an empty or absent database would degrade
    # to "no company matches anything" rather than to a slower fallback.
    database_url: str
    openai_api_key: str
    openai_model: str
    max_research_turns: int
    max_auditor_turns: int
    web_search_context_size: str
    openai_agents_tracing_enabled: bool = False
    # Companies House migrated these hosts from the bare *.gov.uk domain to
    # *.company-information.service.gov.uk; the old host no longer resolves.
    companies_house_base_url: str = "https://api.company-information.service.gov.uk"
    companies_house_public_base_url: str = "https://find-and-update.company-information.service.gov.uk"


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required environment variable '{name}'. "
            f"Copy .env.example to .env and fill it in."
        )
    return value


def _choice(name: str, default: str, allowed: set[str]) -> str:
    value = os.environ.get(name, default).strip().lower()
    if value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise RuntimeError(f"'{name}' must be one of: {choices}.")
    return value


def _positive_int(name: str, default: str, fallback_name: str | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None and fallback_name is not None:
        raw = os.environ.get(fallback_name)
    value = int(raw or default)
    if value < 1:
        raise RuntimeError(f"'{name}' must be at least 1.")
    return value


def _boolean(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(
        f"'{name}' must be one of: true, false, 1, 0, yes, no, on, off."
    )


def load_settings() -> Settings:
    return Settings(
        companies_house_api_key=_require("COMPANIES_HOUSE_API_KEY"),
        database_url=_require("DATABASE_URL"),
        openai_api_key=_require("OPENAI_API_KEY"),
        openai_model=os.environ.get("OPENAI_MODEL", "gpt-4.1"),
        max_research_turns=_positive_int(
            "MAX_RESEARCH_TURNS",
            "8",
            fallback_name="MAX_RESEARCH_TOOL_CALLS",
        ),
        max_auditor_turns=_positive_int("MAX_AUDITOR_TURNS", "2"),
        web_search_context_size=_choice(
            "WEB_SEARCH_CONTEXT_SIZE",
            "medium",
            {"low", "medium", "high"},
        ),
        openai_agents_tracing_enabled=_boolean(
            "OPENAI_AGENTS_TRACING_ENABLED",
            default=False,
        ),
    )
