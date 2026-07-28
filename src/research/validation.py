"""Mechanical grounding checks around the LLM-based evidence audit."""

from __future__ import annotations

from typing import Any

from src.research.schemas import (
    Confidence,
    Contradiction,
    EvidenceAudit,
    KeyPoint,
    RemovedClaim,
    ReportSection,
    SpecialistFindings,
    StructuredReport,
)
from src.research.sources import SourceRegistry


def register_run_sources(run_result: Any, registry: SourceRegistry) -> int:
    """Register URLs that occur in the SDK's raw web-search response payloads.

    ``response_include=["web_search_call.action.sources"]`` supplies the search
    sources. URL-citation annotations are captured as well because they include
    better human-readable titles.
    """
    calls = 0
    for response in getattr(run_result, "raw_responses", ()) or ():
        for item in getattr(response, "output", ()) or ():
            item_type = getattr(item, "type", None)
            if item_type == "web_search_call":
                calls += 1
                action = getattr(item, "action", None)
                for source in getattr(action, "sources", None) or ():
                    url = getattr(source, "url", None)
                    if url:
                        registry.add("web", url, url)
                action_url = getattr(action, "url", None)
                if action_url:
                    registry.add("web", action_url, action_url)
            elif item_type == "message":
                for content in getattr(item, "content", ()) or ():
                    for annotation in getattr(content, "annotations", None) or ():
                        if getattr(annotation, "type", None) != "url_citation":
                            continue
                        url = getattr(annotation, "url", None)
                        if url:
                            registry.add(
                                "web",
                                getattr(annotation, "title", None) or url,
                                url,
                            )
    return calls


def validate_specialist_findings(
    findings: SpecialistFindings,
    registry: SourceRegistry,
) -> tuple[ReportSection, list[RemovedClaim]]:
    """Translate cited URLs to IDs and reject claims with no searched source."""
    points: list[KeyPoint] = []
    removed: list[RemovedClaim] = []
    for finding in findings.claims:
        source_ids: list[int] = []
        for citation in finding.citations:
            source_id = registry.id_for_url(citation.url)
            if source_id is not None and source_id not in source_ids:
                registry.add("web", citation.title, citation.url)
                source_ids.append(source_id)
        if not source_ids:
            removed.append(
                RemovedClaim(
                    claim=finding.claim,
                    reason=(
                        "No citation URL matched a source in the raw web-search "
                        "payload; rejected before evidence audit."
                    ),
                )
            )
            continue
        points.append(
            KeyPoint(
                claim=finding.claim,
                source_ids=source_ids,
                confidence=finding.confidence,
            )
        )
    gaps = list(findings.evidence_gaps)
    if findings.claims and not points:
        gaps.append("No specialist claims passed programmatic citation validation.")
    validated_summary = (
        " ".join(point.claim for point in points)
        if points
        else "No validated specialist evidence was available for this track."
    )
    return (
        ReportSection(
            summary=validated_summary,
            key_points=points,
            confidence=findings.confidence if points else Confidence.low,
            evidence_gaps=gaps,
        ),
        removed,
    )


def validate_audit(
    audit: EvidenceAudit,
    registry: SourceRegistry,
    allowed_ids: set[int] | None = None,
) -> EvidenceAudit:
    """Remove any evidence the auditor failed to keep inside the closed registry."""
    valid_ids = registry.valid_ids()
    if allowed_ids is not None:
        valid_ids &= allowed_ids

    invalid_audit_claims: list[RemovedClaim] = []

    def clean_section(section: ReportSection) -> ReportSection:
        points: list[KeyPoint] = []
        for point in section.key_points:
            ids = list(dict.fromkeys(sid for sid in point.source_ids if sid in valid_ids))
            if ids:
                points.append(
                    KeyPoint(
                        claim=point.claim,
                        source_ids=ids,
                        confidence=point.confidence,
                    )
                )
            else:
                invalid_audit_claims.append(
                    RemovedClaim(
                        claim=point.claim,
                        reason=(
                            "The evidence auditor attached no source ID allowed by "
                            "the validated specialist evidence."
                        ),
                    )
                )
        gaps = list(section.evidence_gaps)
        if section.key_points and not points:
            gaps.append("The evidence auditor returned no claims with valid source IDs.")
        return ReportSection(
            summary=(
                section.summary
                if points
                else "No validated evidence was available for this section."
            ),
            key_points=points,
            confidence=section.confidence if points else Confidence.low,
            evidence_gaps=gaps,
        )

    contradictions = [
        Contradiction(
            topic=item.topic,
            description=item.description,
            source_ids=list(
                dict.fromkeys(sid for sid in item.source_ids if sid in valid_ids)
            ),
        )
        for item in audit.contradictions
    ]
    return EvidenceAudit(
        business_model=clean_section(audit.business_model),
        competitive_landscape=clean_section(audit.competitive_landscape),
        quality_signals=clean_section(audit.quality_signals),
        duplicates_merged=list(audit.duplicates_merged),
        contradictions=contradictions,
        removed_claims=list(audit.removed_claims) + invalid_audit_claims,
    )


def drop_invalid_citations(
    report: StructuredReport,
    registry: SourceRegistry,
) -> StructuredReport:
    """Compatibility helper for callers that only have a StructuredReport."""
    placeholder = EvidenceAudit(
        business_model=report.business_model,
        competitive_landscape=report.competitive_landscape,
        quality_signals=report.quality_signals,
        duplicates_merged=[],
        contradictions=[],
        removed_claims=[],
    )
    return validate_audit(placeholder, registry).structured_report()
