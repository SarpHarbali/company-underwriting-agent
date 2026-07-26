"""Assembles the final markdown report from Companies House facts + the
structured research output. Pure formatting - no LLM calls here, so the
rendered report is a deterministic function of already-grounded data."""

from __future__ import annotations

from typing import Any

from src.research.agent import ResearchResult
from src.research.schemas import ReportSection


def _fmt_address(address: dict[str, Any] | None) -> str:
    if not address:
        return "Not available"
    parts = [
        address.get("address_line_1"),
        address.get("address_line_2"),
        address.get("locality"),
        address.get("region"),
        address.get("postal_code"),
        address.get("country"),
    ]
    return ", ".join(p for p in parts if p) or "Not available"


def build_official_record_markdown(company_profile: dict[str, Any], public_url: str) -> str:
    sic_codes = company_profile.get("sic_codes") or []
    previous_names = company_profile.get("previous_company_names") or []

    lines = [
        f"**Company name:** {company_profile.get('company_name', 'Unknown')}",
        f"**Company number:** {company_profile.get('company_number', 'Unknown')}",
        f"**Status:** {company_profile.get('company_status', 'Unknown')}",
        f"**Type:** {company_profile.get('type', 'Unknown')}",
        f"**Incorporated:** {company_profile.get('date_of_creation', 'Unknown')}",
        f"**SIC codes:** {', '.join(sic_codes) if sic_codes else 'None listed'}",
        f"**Registered office:** {_fmt_address(company_profile.get('registered_office_address'))}",
        f"**Companies House record:** [{public_url}]({public_url})",
    ]
    if previous_names:
        names = "; ".join(
            f"{n.get('name', '?')} (until {n.get('ceased_on', '?')})" for n in previous_names
        )
        lines.append(f"**Previous names:** {names}")
    return "\n\n".join(lines)


def _confidence_label(confidence: Any) -> str:
    return str(getattr(confidence, "value", confidence)).upper()


def _render_section(title: str, section: ReportSection) -> str:
    lines = [f"## {title}  _(confidence: {_confidence_label(section.confidence)})_", "", section.summary, ""]
    if section.key_points:
        for point in section.key_points:
            refs = "".join(f"[{sid}]" for sid in point.source_ids) or "[no direct source]"
            lines.append(f"- {point.claim} {refs} _(confidence: {_confidence_label(point.confidence)})_")
        lines.append("")
    if section.evidence_gaps:
        lines.append("**Evidence gaps for this section:**")
        for gap in section.evidence_gaps:
            lines.append(f"- {gap}")
        lines.append("")
    return "\n".join(lines)


def build_report_markdown(
    company_profile: dict[str, Any],
    public_url: str,
    research_result: ResearchResult,
) -> str:
    report = research_result.report
    name = company_profile.get("company_name", "Unknown company")
    number = company_profile.get("company_number", "unknown")

    parts = [
        f"# Underwriting Intelligence Report: {name}",
        f"*Company number {number} - generated from Companies House data and public web research.*",
        "",
        "---",
        "",
        _render_section("1. Business Model Summary", report.business_model),
        _render_section("2. Competitive Landscape", report.competitive_landscape),
        _render_section("3. Company Quality Signals", report.quality_signals),
        "---",
        "",
        "## Data Completeness & Caveats",
        "",
    ]

    all_gaps = (
        report.business_model.evidence_gaps
        + report.competitive_landscape.evidence_gaps
        + report.quality_signals.evidence_gaps
    )
    if all_gaps:
        for gap in all_gaps:
            parts.append(f"- {gap}")
    else:
        parts.append("- No specific evidence gaps were flagged by the research agent.")

    if research_result.hit_tool_call_budget:
        parts.append(
            "- The research tool-call budget was reached before the agent indicated it was "
            "finished; treat this report as a partial pass and consider re-running with a "
            "higher budget for a company this complex."
        )
    parts.append(
        f"- This report draws on {len(research_result.sources)} distinct sources "
        f"({research_result.tool_call_count} research tool calls). Web sources reflect a "
        f"single point-in-time automated search and have not been independently verified."
    )
    parts.append("")

    parts.append("## References")
    parts.append("")
    if research_result.sources.all():
        for source in research_result.sources.all():
            kind_label = "Companies House" if source.kind == "companies_house" else "Web"
            parts.append(f"[{source.id}] ({kind_label}) [{source.title}]({source.url})")
    else:
        parts.append("No sources were gathered.")

    return "\n".join(parts)
