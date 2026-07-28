import asyncio
from types import SimpleNamespace

from src.config import Settings
from src.research.agent import _run_research_async
from src.research.schemas import (
    Confidence,
    EvidenceAudit,
    KeyPoint,
    ReportSection,
    SourceCitation,
    SpecialistClaim,
    SpecialistFindings,
)


def _section(claim: str, source_id: int) -> ReportSection:
    return ReportSection(
        summary=claim,
        key_points=[
            KeyPoint(
                claim=claim,
                source_ids=[source_id],
                confidence=Confidence.medium,
            )
        ],
        confidence=Confidence.medium,
        evidence_gaps=[],
    )


def _raw_web_result(url: str):
    action = SimpleNamespace(sources=[], url=None)
    call = SimpleNamespace(type="web_search_call", action=action)
    annotation = SimpleNamespace(type="url_citation", title=url, url=url)
    content = SimpleNamespace(annotations=[annotation])
    message = SimpleNamespace(type="message", content=[content])
    response = SimpleNamespace(output=[call, message])
    return [response]


def test_specialists_run_in_parallel_before_auditor(monkeypatch):
    started = 0
    structured = 0
    all_started = asyncio.Event()
    auditor_started_after_specialists = False

    async def fake_run(starting_agent, input, **kwargs):
        nonlocal started, structured, auditor_started_after_specialists
        assert kwargs["run_config"].tracing_disabled is True
        if starting_agent.name == "Underwriting evidence auditor":
            auditor_started_after_specialists = started == 3 and structured == 3
            return SimpleNamespace(
                final_output=EvidenceAudit(
                    business_model=_section("Business evidence.", 2),
                    competitive_landscape=_section("Competition evidence.", 3),
                    quality_signals=_section("Quality evidence.", 4),
                    duplicates_merged=["duplicate"],
                    contradictions=[],
                    removed_claims=[],
                ),
                raw_responses=[],
            )

        track_slug = starting_agent.name.split()[0].lower()
        url = f"https://evidence.example/{track_slug}"
        if "structured findings" not in starting_agent.name:
            assert "Jane Doe" in input
            started += 1
            if started == 3:
                all_started.set()
            await asyncio.wait_for(all_started.wait(), timeout=1)
            return SimpleNamespace(
                final_output=f"{track_slug} cited research notes",
                raw_responses=_raw_web_result(url),
            )

        structured += 1
        return SimpleNamespace(
            final_output=SpecialistFindings(
                summary=f"{track_slug} summary",
                claims=[
                    SpecialistClaim(
                        claim=f"{track_slug} claim",
                        citations=[SourceCitation(title=track_slug, url=url)],
                        confidence=Confidence.medium,
                    )
                ],
                confidence=Confidence.medium,
                evidence_gaps=[],
            ),
            raw_responses=[],
        )

    monkeypatch.setattr("src.research.agent.Runner.run", fake_run)
    ch_client = SimpleNamespace(
        get_officers=lambda number: [
            {"name": "Jane Doe", "officer_role": "director", "appointed_on": "2020-01-01"}
        ],
        get_filing_history=lambda number: [],
        get_persons_with_significant_control=lambda number: [],
        get_charges=lambda number: [],
    )
    settings = Settings(
        companies_house_api_key="ch-test",
        database_url="postgresql://unused",
        openai_api_key="openai-test",
        openai_model="gpt-4.1",
        max_research_turns=4,
        max_auditor_turns=2,
        web_search_context_size="medium",
    )
    profile = {
        "company_name": "Acme Ltd",
        "company_number": "123",
        "company_status": "active",
    }

    result = asyncio.run(
        _run_research_async(
            settings,
            ch_client,
            profile,
            "https://find-and-update.company-information.service.gov.uk/company/123",
            lambda _: None,
        )
    )

    assert auditor_started_after_specialists
    assert result.agent_run_count == 7
    assert result.tool_call_count == 3
    assert any(source.url.endswith("/officers") for source in result.sources.all())
    assert result.audit is not None
    assert result.audit.duplicates_merged == ["duplicate"]
