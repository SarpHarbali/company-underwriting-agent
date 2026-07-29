from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict


class Confidence(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


class KeyPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim: str
    source_ids: list[int]
    confidence: Confidence


class ReportSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    key_points: list[KeyPoint]
    confidence: Confidence
    evidence_gaps: list[str]


class StructuredReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    business_model: ReportSection
    competitive_landscape: ReportSection
    quality_signals: ReportSection


class SourceCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    url: str


class SpecialistClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim: str
    citations: list[SourceCitation]
    confidence: Confidence


class SpecialistFindings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    claims: list[SpecialistClaim]
    confidence: Confidence
    evidence_gaps: list[str]


class Contradiction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str
    description: str
    source_ids: list[int]


class RemovedClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim: str
    reason: str


class EvidenceAudit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    business_model: ReportSection
    competitive_landscape: ReportSection
    quality_signals: ReportSection
    duplicates_merged: list[str]
    contradictions: list[Contradiction]
    removed_claims: list[RemovedClaim]

    def structured_report(self) -> StructuredReport:
        return StructuredReport(
            business_model=self.business_model,
            competitive_landscape=self.competitive_landscape,
            quality_signals=self.quality_signals,
        )
