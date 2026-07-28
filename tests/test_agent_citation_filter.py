from src.research.agent import _drop_invalid_citations
from src.research.schemas import Confidence, KeyPoint, ReportSection, StructuredReport
from src.research.sources import SourceRegistry


def test_drop_invalid_citations_strips_unknown_ids():
    registry = SourceRegistry()
    registry.add("web", "Real source", "https://example.com/real")

    report = StructuredReport(
        business_model=ReportSection(
            summary="summary",
            key_points=[
                KeyPoint(claim="Real claim", source_ids=[1, 42], confidence=Confidence.high),
                KeyPoint(claim="Unsupported", source_ids=[42], confidence=Confidence.low),
            ],
            confidence=Confidence.medium,
            evidence_gaps=[],
        ),
        competitive_landscape=ReportSection(
            summary="s", key_points=[], confidence=Confidence.low, evidence_gaps=[]
        ),
        quality_signals=ReportSection(
            summary="s", key_points=[], confidence=Confidence.low, evidence_gaps=[]
        ),
    )

    cleaned = _drop_invalid_citations(report, registry)
    assert cleaned.business_model.key_points[0].source_ids == [1]
    assert [point.claim for point in cleaned.business_model.key_points] == ["Real claim"]
