from types import SimpleNamespace

from src.research.schemas import (
    Confidence,
    EvidenceAudit,
    KeyPoint,
    ReportSection,
    SourceCitation,
    SpecialistClaim,
    SpecialistFindings,
)
from src.research.sources import SourceRegistry
from src.research.validation import (
    register_official_findings,
    register_run_sources,
    validate_audit,
    validate_specialist_findings,
)


def test_specialist_claim_requires_url_from_raw_search_payload():
    registry = SourceRegistry()
    result = SimpleNamespace(
        raw_responses=[
            SimpleNamespace(
                output=[
                    SimpleNamespace(
                        type="web_search_call",
                        action=SimpleNamespace(
                            sources=[SimpleNamespace(url="https://example.com/real")],
                            url=None,
                        ),
                    )
                ]
            )
        ]
    )
    assert register_run_sources(result, registry) == 1

    findings = SpecialistFindings(
        summary="summary",
        claims=[
            SpecialistClaim(
                claim="Grounded",
                citations=[
                    SourceCitation(title="Real", url="https://example.com/real")
                ],
                confidence=Confidence.high,
            ),
            SpecialistClaim(
                claim="Fabricated citation",
                citations=[
                    SourceCitation(title="Fake", url="https://example.com/invented")
                ],
                confidence=Confidence.low,
            ),
        ],
        confidence=Confidence.medium,
        evidence_gaps=[],
    )

    section, removed = validate_specialist_findings(findings, registry)

    assert [point.claim for point in section.key_points] == ["Grounded"]
    assert section.key_points[0].source_ids == [1]
    assert [item.claim for item in removed] == ["Fabricated citation"]


def test_empty_validated_track_cannot_retain_high_confidence():
    registry = SourceRegistry()
    findings = SpecialistFindings(
        summary="Confident but unsupported.",
        claims=[
            SpecialistClaim(
                claim="Unsupported",
                citations=[SourceCitation(title="Missing", url="https://missing.test")],
                confidence=Confidence.high,
            )
        ],
        confidence=Confidence.high,
        evidence_gaps=[],
    )

    section, _ = validate_specialist_findings(findings, registry)

    assert section.key_points == []
    assert section.confidence is Confidence.low


def test_official_findings_register_citations_without_raw_search_evidence():
    registry = SourceRegistry()
    findings = SpecialistFindings(
        summary="Filings summary",
        claims=[
            SpecialistClaim(
                claim="No registered charges were found.",
                citations=[
                    SourceCitation(
                        title="Companies House - Charges",
                        url=(
                            "https://find-and-update.company-information.service.gov.uk"
                            "/company/123/charges"
                        ),
                    )
                ],
                confidence=Confidence.high,
            )
        ],
        confidence=Confidence.high,
        evidence_gaps=["No officer records were found for this company."],
    )

    section = register_official_findings(findings, registry)

    assert len(section.key_points) == 1
    assert section.key_points[0].source_ids == [1]
    assert registry.get(1).kind == "companies_house"
    assert section.evidence_gaps == findings.evidence_gaps


def test_audit_removes_claims_without_allowed_source_ids():
    registry = SourceRegistry()
    registry.add("web", "Allowed", "https://example.com/allowed")
    section = ReportSection(
        summary="summary",
        key_points=[
            KeyPoint(
                claim="Grounded",
                source_ids=[1, 42],
                confidence=Confidence.high,
            ),
            KeyPoint(
                claim="Unsupported",
                source_ids=[42],
                confidence=Confidence.low,
            ),
        ],
        confidence=Confidence.medium,
        evidence_gaps=[],
    )
    empty_section = ReportSection(
        summary="No findings",
        key_points=[],
        confidence=Confidence.low,
        evidence_gaps=[],
    )
    audit = EvidenceAudit(
        business_model=section,
        competitive_landscape=empty_section,
        quality_signals=empty_section,
        duplicates_merged=[],
        contradictions=[],
        removed_claims=[],
    )

    validated = validate_audit(audit, registry, allowed_ids={1})

    assert [point.claim for point in validated.business_model.key_points] == [
        "Grounded"
    ]
    assert validated.business_model.key_points[0].source_ids == [1]
    assert [claim.claim for claim in validated.removed_claims] == ["Unsupported"]
