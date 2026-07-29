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
    return ", ".join(part for part in parts if part) or "Not available"


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
            f"{previous_name.get('name', '?')} "
            f"(until {previous_name.get('ceased_on', '?')})"
            for previous_name in previous_names
        )
        lines.append(f"**Previous names:** {names}")
    return "\n\n".join(lines)


def _confidence_label(confidence: Any) -> str:
    return str(getattr(confidence, "value", confidence)).upper()


def _render_section(title: str, section: ReportSection) -> str:
    summary_ids = list(
        dict.fromkeys(
            source_id
            for point in section.key_points
            for source_id in point.source_ids
        )
    )
    summary_refs = (
        "".join(f"[{source_id}]" for source_id in summary_ids)
        or "[no validated evidence]"
    )
    lines = [
        f"## {title}  _(confidence: {_confidence_label(section.confidence)})_",
        "",
        f"{section.summary} {summary_refs}",
        "",
    ]
    if section.key_points:
        for point in section.key_points:
            refs = (
                "".join(f"[{source_id}]" for source_id in point.source_ids)
                or "[no direct source]"
            )
            lines.append(
                f"- {point.claim} {refs} "
                f"_(confidence: {_confidence_label(point.confidence)})_"
            )
        lines.append("")
    return "\n".join(lines)


def build_report_markdown(
    company_profile: dict[str, Any],
    research_result: ResearchResult,
) -> str:
    report = research_result.report
    name = company_profile.get("company_name", "Unknown company")
    number = company_profile.get("company_number", "unknown")
    referenced_sources = [
        source
        for source in research_result.sources.all()
        if research_result.validated_source_ids is None
        or source.id in research_result.validated_source_ids
    ]

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

    all_gaps = list(
        dict.fromkeys(
            report.business_model.evidence_gaps
            + report.competitive_landscape.evidence_gaps
            + report.quality_signals.evidence_gaps
        )
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
        f"- This report draws on {len(referenced_sources)} distinct validated sources "
        f"({research_result.tool_call_count} web-search calls across "
        f"{research_result.agent_run_count or 'multiple'} agent runs). Web sources reflect a "
        f"single point-in-time automated search and have not been independently verified."
    )
    for warning in research_result.research_warnings:
        parts.append(f"- Research warning: {warning}")
    parts.append("")

    if research_result.audit is not None:
        parts.append("## Evidence Audit")
        parts.append("")
        parts.append(
            f"- Duplicate groups merged: {len(research_result.audit.duplicates_merged)}"
        )
        parts.append(
            f"- Unsupported or unsuitable claims removed: "
            f"{len(research_result.audit.removed_claims)}"
        )
        if research_result.audit.contradictions:
            parts.append("- Material contradictions flagged:")
            for contradiction in research_result.audit.contradictions:
                refs = "".join(
                    f"[{source_id}]" for source_id in contradiction.source_ids
                )
                parts.append(
                    f"  - **{contradiction.topic}:** {contradiction.description} "
                    f"{refs}".rstrip()
                )
        else:
            parts.append("- No material contradictions were flagged by the evidence auditor.")
        parts.append("")

    parts.append("## References")
    parts.append("")
    if referenced_sources:
        for source in referenced_sources:
            kind_label = "Companies House" if source.kind == "companies_house" else "Web"
            parts.append(f"- [{source.id}] ({kind_label}) [{source.title}]({source.url})")
    else:
        parts.append("No sources were gathered.")

    return "\n".join(parts)
