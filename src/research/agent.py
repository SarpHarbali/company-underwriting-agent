"""Parallel specialist research followed by a closed-registry evidence audit.

Three OpenAI Agents SDK specialists research independent report tracks in
parallel. Their URL citations are accepted only when the URL occurs in the raw
hosted web-search response. A fourth agent then audits the resulting ID-backed
evidence, merges duplicates, flags contradictions and removes unsupported
claims. The report builder sees only that audited output.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol

from agents import Agent, ModelSettings, RunConfig, Runner, WebSearchTool
from agents.models.openai_provider import OpenAIProvider

from src.companies_house.client import CompaniesHouseClient
from src.config import Settings
from src.research.prompts import (
    AUDITOR_INSTRUCTIONS,
    RESEARCH_TRACKS,
    ResearchTrack,
    auditor_input,
    specialist_input,
    specialist_instructions,
    specialist_structuring_input,
    specialist_structuring_instructions,
)
from src.research.schemas import (
    Confidence,
    EvidenceAudit,
    RemovedClaim,
    ReportSection,
    SpecialistFindings,
    StructuredReport,
)
from src.research.sources import SourceRegistry
from src.research.validation import (
    drop_invalid_citations,
    register_run_sources,
    validate_audit,
    validate_specialist_findings,
)


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
    audit: EvidenceAudit | None = None
    agent_run_count: int = 0
    research_warnings: list[str] = field(default_factory=list)
    validated_source_ids: set[int] | None = None


@dataclass
class _SpecialistRun:
    track: ResearchTrack
    findings: SpecialistFindings
    run_result: Any | None
    warning: str | None = None
    agent_run_count: int = 0


def _empty_findings(track: ResearchTrack, reason: str) -> SpecialistFindings:
    return SpecialistFindings(
        summary=f"{track.label} research was incomplete.",
        claims=[],
        confidence=Confidence.low,
        evidence_gaps=[reason],
    )


async def _run_specialist(
    track: ResearchTrack,
    settings: Settings,
    company_profile: dict[str, Any],
    public_url: str,
    run_config: RunConfig,
    progress: ProgressCallback,
) -> _SpecialistRun:
    agent = Agent(
        name=f"{track.label} research specialist",
        instructions=specialist_instructions(track),
        model=settings.openai_model,
        tools=[WebSearchTool(search_context_size=settings.web_search_context_size)],
        model_settings=ModelSettings(
            tool_choice="required",
            truncation="auto",
        ),
    )
    research_result: Any | None = None
    try:
        research_result = await Runner.run(
            agent,
            specialist_input(track, company_profile, public_url),
            max_turns=settings.max_research_turns,
            run_config=run_config,
        )
        local_registry = SourceRegistry()
        local_registry.add(
            "companies_house",
            f"{company_profile.get('company_name', company_profile['company_number'])} "
            "- Company overview",
            public_url,
        )
        register_run_sources(research_result, local_registry)
        captured_sources = [
            {"title": source.title, "url": source.url}
            for source in local_registry.all()
        ]
        structuring_agent = agent.clone(
            name=f"{track.label} specialist - structured findings",
            instructions=specialist_structuring_instructions(track),
            tools=[],
            model_settings=ModelSettings(truncation="auto"),
            output_type=SpecialistFindings,
        )
        structured_result = await Runner.run(
            structuring_agent,
            specialist_structuring_input(
                track,
                str(research_result.final_output),
                captured_sources,
            ),
            max_turns=settings.max_auditor_turns,
            run_config=run_config,
        )
        findings = structured_result.final_output
        if not isinstance(findings, SpecialistFindings):
            findings = SpecialistFindings.model_validate(findings)
        progress(f"{track.label} research complete.")
        return _SpecialistRun(
            track=track,
            findings=findings,
            run_result=research_result,
            agent_run_count=2,
        )
    except Exception as exc:  # noqa: BLE001 - partial tracks are surfaced as gaps
        reason = f"{track.label} specialist failed: {type(exc).__name__}: {exc}"
        progress(reason)
        return _SpecialistRun(
            track=track,
            findings=_empty_findings(track, reason),
            run_result=research_result,
            warning=reason,
            agent_run_count=1 if research_result is not None else 0,
        )


async def _run_research_async(
    settings: Settings,
    company_profile: dict[str, Any],
    public_url: str,
    progress: ProgressCallback,
) -> ResearchResult:
    company_title = company_profile.get("company_name", company_profile["company_number"])
    registry = SourceRegistry()
    registry.add(
        "companies_house",
        f"{company_title} - Company overview",
        public_url,
    )

    provider = OpenAIProvider(api_key=settings.openai_api_key)
    specialist_config = RunConfig(
        model_provider=provider,
        tracing_disabled=not settings.openai_agents_tracing_enabled,
        workflow_name="Underwriting specialist research",
        trace_metadata={
            "company_number": str(company_profile["company_number"]),
            "stage": "parallel-specialists",
        },
    )

    progress("Starting three specialist web-research agents in parallel...")
    specialist_runs = await asyncio.gather(
        *(
            _run_specialist(
                track,
                settings,
                company_profile,
                public_url,
                specialist_config,
                progress,
            )
            for track in RESEARCH_TRACKS
        )
    )

    web_search_calls = 0
    for specialist_run in specialist_runs:
        if specialist_run.run_result is not None:
            web_search_calls += register_run_sources(
                specialist_run.run_result,
                registry,
            )

    evidence: dict[str, Any] = {}
    pre_audit_removals: list[RemovedClaim] = []
    warnings: list[str] = []
    for specialist_run in specialist_runs:
        section, removed = validate_specialist_findings(
            specialist_run.findings,
            registry,
        )
        evidence[specialist_run.track.key] = section.model_dump(mode="json")
        pre_audit_removals.extend(removed)
        if specialist_run.warning:
            warnings.append(specialist_run.warning)

    auditor_source_ids = {
        source_id
        for section_data in evidence.values()
        for point in section_data["key_points"]
        for source_id in point["source_ids"]
    }
    sources = [
        {
            "id": source.id,
            "kind": source.kind,
            "title": source.title,
            "url": source.url,
        }
        for source in registry.all()
        if source.id in auditor_source_ids
    ]
    auditor = Agent(
        name="Underwriting evidence auditor",
        instructions=AUDITOR_INSTRUCTIONS,
        model=settings.openai_model,
        output_type=EvidenceAudit,
    )
    progress("Auditing evidence, resolving duplicates and checking contradictions...")
    audit_result = await Runner.run(
        auditor,
        auditor_input(company_profile, public_url, evidence, sources),
        max_turns=settings.max_auditor_turns,
        run_config=RunConfig(
            model_provider=provider,
            tracing_disabled=not settings.openai_agents_tracing_enabled,
            workflow_name="Underwriting evidence audit",
            trace_metadata={
                "company_number": str(company_profile["company_number"]),
                "stage": "evidence-audit",
            },
        ),
    )
    audit = audit_result.final_output
    if not isinstance(audit, EvidenceAudit):
        audit = EvidenceAudit.model_validate(audit)
    audit.removed_claims = pre_audit_removals + audit.removed_claims
    audit = validate_audit(audit, registry, allowed_ids=auditor_source_ids)
    progress("Evidence audit complete.")

    validated_source_ids = {
        source_id
        for section in (
            audit.business_model,
            audit.competitive_landscape,
            audit.quality_signals,
        )
        for point in section.key_points
        for source_id in point.source_ids
    }
    validated_source_ids.update(
        source_id
        for contradiction in audit.contradictions
        for source_id in contradiction.source_ids
    )

    return ResearchResult(
        report=audit.structured_report(),
        sources=registry,
        transcript_notes=[],
        tool_call_count=web_search_calls,
        hit_tool_call_budget=any(
            "MaxTurnsExceeded" in warning for warning in warnings
        ),
        audit=audit,
        agent_run_count=sum(run.agent_run_count for run in specialist_runs) + 1,
        research_warnings=warnings,
        validated_source_ids=validated_source_ids,
    )


def run_research(
    ch_client: CompaniesHouseClient,
    settings: Settings,
    company_profile: dict[str, Any],
    progress: ProgressCallback = _noop_progress,
) -> ResearchResult:
    """Run the complete synchronous report-research workflow."""
    company_number = company_profile["company_number"]
    public_url = ch_client.public_company_url(company_number)
    return asyncio.run(
        _run_research_async(
            settings=settings,
            company_profile=company_profile,
            public_url=public_url,
            progress=progress,
        )
    )


def _drop_invalid_citations(
    report: StructuredReport,
    registry: SourceRegistry,
) -> StructuredReport:
    """Backward-compatible name for the closed-registry citation filter."""
    return drop_invalid_citations(report, registry)
