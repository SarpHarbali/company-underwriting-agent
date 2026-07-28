"""Coordinate company resolution, research and report rendering."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from openai import OpenAI

from src.companies_house.client import CompaniesHouseClient
from src.companies_house.name_index import PostgresNameRepository
from src.companies_house.query_suggester import build_query_suggester
from src.companies_house.resolver import (
    ResolutionResult,
    SuggestedAlternative,
    resolve_company,
    suggest_alternative,
)
from src.config import Settings, load_settings
from src.db import create_pool
from src.report.builder import build_report_markdown
from src.research.agent import ProgressCallback, ResearchResult, run_research


@dataclass
class GeneratedReport:
    markdown: str
    research_result: ResearchResult


class Orchestrator:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or load_settings()
        self.ch_client = CompaniesHouseClient(
            api_key=self.settings.companies_house_api_key,
            base_url=self.settings.companies_house_base_url,
            public_base_url=self.settings.companies_house_public_base_url,
        )
        self.db_pool = create_pool(self.settings.database_url)
        self.name_repository = PostgresNameRepository(self.db_pool)
        self.openai_client = OpenAI(api_key=self.settings.openai_api_key)
        self._suggest_query = build_query_suggester(
            self.openai_client, self.settings.openai_model
        )

    def resolve(self, user_input: str) -> ResolutionResult:
        return resolve_company(
            self.name_repository,
            self.ch_client,
            user_input,
            suggest_query=self._suggest_query,
        )

    def suggest_alternative(
        self, user_input: str, listed_numbers: Collection[str] = ()
    ) -> SuggestedAlternative | None:
        return suggest_alternative(
            self.name_repository, user_input, self._suggest_query, listed_numbers
        )

    def get_company_profile(self, company_number: str) -> dict:
        return self.ch_client.get_company_profile(company_number)

    def generate_report(
        self,
        company_profile: dict,
        progress: ProgressCallback = lambda _: None,
    ) -> GeneratedReport:
        result = run_research(
            ch_client=self.ch_client,
            settings=self.settings,
            company_profile=company_profile,
            progress=progress,
        )
        markdown = build_report_markdown(company_profile, result)
        return GeneratedReport(markdown=markdown, research_result=result)
