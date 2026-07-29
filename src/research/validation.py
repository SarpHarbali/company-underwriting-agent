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
)
from src.research.sources import SourceRegistry


def register_run_sources(run_result: Any, registry: SourceRegistry) -> int:
    web_search_calls = 0
    for response in getattr(run_result, "raw_responses", ()) or ():
        for item in getattr(response, "output", ()) or ():
            item_type = getattr(item, "type", None)
            if item_type == "web_search_call":
                web_search_calls += 1
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
    return web_search_calls


def validate_specialist_findings(
    findings: SpecialistFindings,
    registry: SourceRegistry,
) -> tuple[ReportSection, list[RemovedClaim]]:
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


def register_official_findings(
    findings: SpecialistFindings,
    registry: SourceRegistry,
) -> ReportSection:
    points: list[KeyPoint] = []
    for finding in findings.claims:
        source_ids = list(
            dict.fromkeys(
                registry.add("companies_house", citation.title, citation.url)
                for citation in finding.citations
            )
        )
        points.append(
            KeyPoint(
                claim=finding.claim,
                source_ids=source_ids,
                confidence=finding.confidence,
            )
        )
    return ReportSection(
        summary=findings.summary,
        key_points=points,
        confidence=findings.confidence,
        evidence_gaps=list(findings.evidence_gaps),
    )


def validate_audit(
    audit: EvidenceAudit,
    registry: SourceRegistry,
    allowed_ids: set[int] | None = None,
) -> EvidenceAudit:
    valid_ids = registry.valid_ids()
    if allowed_ids is not None:
        valid_ids &= allowed_ids

    invalid_audit_claims: list[RemovedClaim] = []

    def clean_section(section: ReportSection) -> ReportSection:
        points: list[KeyPoint] = []
        for point in section.key_points:
            source_ids = list(
                dict.fromkeys(
                    source_id
                    for source_id in point.source_ids
                    if source_id in valid_ids
                )
            )
            if source_ids:
                points.append(
                    KeyPoint(
                        claim=point.claim,
                        source_ids=source_ids,
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
                dict.fromkeys(
                    source_id
                    for source_id in item.source_ids
                    if source_id in valid_ids
                )
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
