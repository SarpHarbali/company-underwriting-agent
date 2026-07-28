"""Thin end-to-end coordinator wiring resolution -> research -> report building.

Kept deliberately dumb: all the interesting logic lives in the modules it calls.
This exists so the Streamlit UI doesn't need to know about OpenAI/Companies House
clients directly.
"""

from __future__ import annotations

from dataclasses import dataclass

from openai import OpenAI

from src.companies_house.client import CompaniesHouseClient
from src.companies_house.query_suggester import build_query_suggester
from src.companies_house.resolver import ResolutionResult, resolve_company
from src.config import Settings, load_settings
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
        self.openai_client = OpenAI(api_key=self.settings.openai_api_key)
        self._suggest_query = build_query_suggester(
            self.openai_client, self.settings.openai_model
        )

    def resolve(self, user_input: str) -> ResolutionResult:
        return resolve_company(self.ch_client, user_input, suggest_query=self._suggest_query)

    def get_company_profile(self, company_number: str) -> dict:
        """Fetch a profile directly - used when the user confirms a disambiguation candidate."""
        return self.ch_client.get_company_profile(company_number)

    def generate_report(
        self,
        company_profile: dict,
        progress: ProgressCallback = lambda _: None,
    ) -> GeneratedReport:
        result = run_research(
            client=self.openai_client,
            ch_client=self.ch_client,
            settings=self.settings,
            company_profile=company_profile,
            progress=progress,
        )
        public_url = self.ch_client.public_company_url(company_profile["company_number"])
        markdown = build_report_markdown(company_profile, public_url, result)
        return GeneratedReport(markdown=markdown, research_result=result)
