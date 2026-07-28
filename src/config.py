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
    openai_web_search_tool_type: str
    max_research_tool_calls: int
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


def load_settings() -> Settings:
    return Settings(
        companies_house_api_key=_require("COMPANIES_HOUSE_API_KEY"),
        database_url=_require("DATABASE_URL"),
        openai_api_key=_require("OPENAI_API_KEY"),
        openai_model=os.environ.get("OPENAI_MODEL", "gpt-4.1"),
        openai_web_search_tool_type=os.environ.get("OPENAI_WEB_SEARCH_TOOL_TYPE", "web_search"),
        max_research_tool_calls=int(os.environ.get("MAX_RESEARCH_TOOL_CALLS", "12")),
    )
