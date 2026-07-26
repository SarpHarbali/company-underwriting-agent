"""Structured output contract for the research agent's final synthesis step.

Passed to the OpenAI Responses API via `responses.parse(text_format=StructuredReport)`,
which enforces this shape at generation time (strict JSON schema mode). Every
`source_ids` entry is expected to reference an ID from the SourceRegistry that was
built during the tool-calling research phase - the report builder drops any that
don't, so an invented citation can never reach the rendered report.
"""

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
