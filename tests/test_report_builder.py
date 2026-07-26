from src.report.builder import build_official_record_markdown, build_report_markdown
from src.research.agent import ResearchResult
from src.research.schemas import Confidence, KeyPoint, ReportSection, StructuredReport
from src.research.sources import SourceRegistry


def _make_section(summary, gaps=None):
    return ReportSection(
        summary=summary,
        key_points=[
            KeyPoint(claim="It sells widgets to UK retailers.", source_ids=[1], confidence=Confidence.high),
            KeyPoint(claim="Made-up unsourced claim.", source_ids=[999], confidence=Confidence.low),
        ],
        confidence=Confidence.medium,
        evidence_gaps=gaps or [],
    )


def _make_research_result():
    registry = SourceRegistry()
    registry.add("companies_house", "Acme Ltd - Filing history", "https://public.example.test/company/123/filing-history")
    report = StructuredReport(
        business_model=_make_section("Acme Ltd sells widgets.", gaps=["No investor filings found."]),
        competitive_landscape=_make_section("Moderately competitive market."),
        quality_signals=_make_section("Limited review data.", gaps=["No Trustpilot presence found."]),
    )
    return ResearchResult(report=report, sources=registry, transcript_notes=["note"], tool_call_count=3)


def test_build_report_markdown_includes_sections_and_references():
    result = _make_research_result()
    profile = {"company_name": "Acme Ltd", "company_number": "123"}
    md = build_report_markdown(profile, "https://public.example.test/company/123", result)

    assert "Acme Ltd" in md
    assert "1. Business Model Summary" in md
    assert "2. Competitive Landscape" in md
    assert "3. Company Quality Signals" in md
    assert "No investor filings found." in md
    assert "## References" in md
    assert "[1]" in md
    # source_ids referencing a non-existent id should still render (builder trusts its input;
    # the agent layer is responsible for stripping invalid ids before this point)
    assert "[999]" in md


def test_hit_budget_flag_adds_caveat():
    result = _make_research_result()
    result.hit_tool_call_budget = True
    profile = {"company_name": "Acme Ltd", "company_number": "123"}
    md = build_report_markdown(profile, "https://public.example.test/company/123", result)
    assert "tool-call budget was reached" in md


def test_build_official_record_markdown_includes_key_fields():
    profile = {
        "company_name": "Acme Ltd",
        "company_number": "123",
        "company_status": "active",
        "type": "ltd",
        "date_of_creation": "2020-01-01",
        "sic_codes": ["62012"],
        "registered_office_address": {"address_line_1": "1 High St", "postal_code": "AB1 2CD"},
    }
    md = build_official_record_markdown(profile, "https://public.example.test/company/123")
    assert "Acme Ltd" in md
    assert "62012" in md
    assert "1 High St" in md
