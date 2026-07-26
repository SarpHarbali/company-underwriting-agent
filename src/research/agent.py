"""Two-phase research agent.

Phase A: the LLM drives a tool-calling loop (Companies House function tools +
OpenAI's hosted web_search tool) deciding for itself what to look up and when it
has gathered enough evidence. Every source it actually consults - a Companies
House page or a URL surfaced by web_search - is logged into a SourceRegistry.

Phase B: a single constrained call (`responses.parse`) turns the research
transcript into the three structured report sections, only allowed to cite
source IDs that exist in the registry built during Phase A.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from openai import OpenAI

from src.companies_house.client import CompaniesHouseClient
from src.config import Settings
from src.research.prompts import SYNTHESIS_SYSTEM_PROMPT, research_system_prompt, synthesis_user_prompt
from src.research.schemas import StructuredReport
from src.research.sources import SourceRegistry
from src.research.tools import TOOL_DEFINITIONS, build_tool_dispatch, call_tool


class ProgressCallback(Protocol):
    def __call__(self, message: str) -> None: ...


def _noop_progress(message: str) -> None:
    pass


@dataclass
class ResearchResult:
    report: StructuredReport
    sources: SourceRegistry
    transcript_notes: list[str] = field(default_factory=list)
    tool_call_count: int = 0
    hit_tool_call_budget: bool = False


def _friendly_tool_label(name: str) -> str:
    return {
        "get_filing_history": "Checking Companies House filing history",
        "get_officers": "Checking Companies House officers",
        "get_persons_with_significant_control": "Checking persons with significant control",
        "get_charges": "Checking registered charges",
    }.get(name, f"Calling {name}")


def run_research(
    client: OpenAI,
    ch_client: CompaniesHouseClient,
    settings: Settings,
    company_profile: dict[str, Any],
    progress: ProgressCallback = _noop_progress,
) -> ResearchResult:
    company_number = company_profile["company_number"]
    company_title = company_profile.get("company_name", company_number)
    public_url = ch_client.public_company_url(company_number)

    registry = SourceRegistry()
    dispatch = build_tool_dispatch(ch_client, company_number, registry, company_title)

    tools = list(TOOL_DEFINITIONS) + [{"type": settings.openai_web_search_tool_type}]
    input_items: list[dict[str, Any]] = [
        {"role": "system", "content": research_system_prompt(company_profile, public_url)},
        {"role": "user", "content": "Begin your research."},
    ]

    transcript_notes: list[str] = []
    tool_call_count = 0
    hit_budget = False

    progress(f"Researching {company_title}...")

    for _ in range(settings.max_research_tool_calls):
        response = client.responses.create(
            model=settings.openai_model,
            input=input_items,
            tools=tools,
        )
        input_items.extend(item.model_dump() for item in response.output)

        had_function_call = False
        for item in response.output:
            if item.type == "function_call":
                had_function_call = True
                tool_call_count += 1
                progress(_friendly_tool_label(item.name))
                result = call_tool(dispatch, item.name, item.arguments)
                input_items.append(
                    {"type": "function_call_output", "call_id": item.call_id, "output": result}
                )
            elif item.type == "web_search_call":
                had_function_call = True
                tool_call_count += 1
                progress("Searching the web...")
            elif item.type == "message":
                for content in item.content:
                    if getattr(content, "type", None) != "output_text":
                        continue
                    transcript_notes.append(content.text)
                    for annotation in getattr(content, "annotations", None) or []:
                        if getattr(annotation, "type", None) == "url_citation":
                            registry.add("web", annotation.title or annotation.url, annotation.url)

        if tool_call_count >= settings.max_research_tool_calls:
            hit_budget = True
            break
        if not had_function_call:
            break

    progress("Synthesising report sections...")
    report = _synthesise(client, settings, transcript_notes, registry)

    return ResearchResult(
        report=report,
        sources=registry,
        transcript_notes=transcript_notes,
        tool_call_count=tool_call_count,
        hit_tool_call_budget=hit_budget,
    )


def _sources_listing(registry: SourceRegistry) -> str:
    if not registry.all():
        return "(no sources were gathered)"
    return "\n".join(f"[{s.id}] {s.title} - {s.url}" for s in registry.all())


def _synthesise(
    client: OpenAI,
    settings: Settings,
    transcript_notes: list[str],
    registry: SourceRegistry,
) -> StructuredReport:
    transcript = "\n\n".join(transcript_notes) or "(the agent produced no interim notes)"
    response = client.responses.parse(
        model=settings.openai_model,
        input=[
            {"role": "system", "content": SYNTHESIS_SYSTEM_PROMPT},
            {"role": "user", "content": synthesis_user_prompt(transcript, _sources_listing(registry))},
        ],
        text_format=StructuredReport,
    )
    report = response.output_parsed
    return _drop_invalid_citations(report, registry)


def _drop_invalid_citations(report: StructuredReport, registry: SourceRegistry) -> StructuredReport:
    """Belt-and-suspenders: strip any source_id the model cited that isn't real."""
    valid_ids = registry.valid_ids()
    for section in (report.business_model, report.competitive_landscape, report.quality_signals):
        for point in section.key_points:
            point.source_ids = [sid for sid in point.source_ids if sid in valid_ids]
    return report
