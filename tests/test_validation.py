from types import SimpleNamespace

from src.research.schemas import (
    Confidence,
    SourceCitation,
    SpecialistClaim,
    SpecialistFindings,
)
from src.research.sources import SourceRegistry
from src.research.validation import (
    register_run_sources,
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
