from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ResearchTrack:
    key: str
    label: str
    objective: str


RESEARCH_TRACKS = (
    ResearchTrack(
        key="business_model",
        label="Business model",
        objective=(
            "Establish what the company actually does, its products/services, how it "
            "makes money, customer types, distribution model, and relevant geographies. "
            "Separate facts from inference and do not infer a business model from SIC "
            "codes alone."
        ),
    ),
    ResearchTrack(
        key="competitive_landscape",
        label="Competitive landscape",
        objective=(
            "Assess the intensity of competition in the company's specific sector and "
            "geographies. Identify relevant competitors or substitutes, barriers to entry, "
            "customer switching or concentration factors, and explain why the resulting "
            "competitive pressure matters to underwriting risk. Go beyond a competitor list."
        ),
    ),
    ResearchTrack(
        key="quality_signals",
        label="Company quality signals",
        objective=(
            "Find and synthesise independent quality indicators such as customer reviews, "
            "trade press, regulator or ombudsman records, awards, complaints, controversies, "
            "and industry reputation. Assess signal strength, representativeness, recency, "
            "and conflicts; absence of evidence is a gap, not a positive signal."
        ),
    ),
)


def company_context(company: dict[str, Any], public_url: str) -> str:
    address = company.get("registered_office_address", {}) or {}
    address_str = ", ".join(
        value
        for value in (
            address.get("address_line_1"),
            address.get("locality"),
            address.get("region"),
            address.get("postal_code"),
            address.get("country"),
        )
        if value
    ) or "unknown"
    return "\n".join(
        (
            f"Legal name: {company.get('company_name', 'unknown')}",
            f"Companies House number: {company.get('company_number', 'unknown')}",
            f"Status: {company.get('company_status', 'unknown')}",
            f"Incorporated: {company.get('date_of_creation', 'unknown')}",
            f"SIC codes: {', '.join(company.get('sic_codes', []) or []) or 'none listed'}",
            f"Registered office: {address_str}",
            f"Verified Companies House profile: {public_url}",
        )
    )


def specialist_instructions(track: ResearchTrack) -> str:
    return f"""You are the {track.label.lower()} specialist in a UK credit-underwriting
research workflow. Work only on your assigned track:

{track.objective}

Research requirements:
- Use web search repeatedly and refine queries when initial results are weak.
- Confirm that every source refers to the verified legal entity; use the company
  number, legal name, trading names, sector and UK geography to avoid namesakes.
- Prefer primary and authoritative sources, then reputable trade press and
  established review/regulatory sources. Treat company-authored material as useful
  for factual descriptions but weak evidence for independent quality judgements.
- Make each factual statement atomic and attach a normal inline web citation.
- Do not include a factual statement with no direct citation. Put missing,
  ambiguous, stale or contradictory matters in an explicit evidence-gaps section.
- Confidence is qualitative: high requires strong corroboration; medium is supported
  but limited; low means a single, indirect, weak, old or potentially biased source.
- Stop once the assigned track is adequately covered or further searches are no
  longer producing material new evidence.

Return concise research notes with inline web citations. A schema-enforced
structuring step will follow, so do not emit JSON."""


def specialist_input(
    track: ResearchTrack,
    company: dict[str, Any],
    public_url: str,
    filings_briefing: str,
) -> str:
    return f"""Research the verified company below for the {track.label.lower()} section.

VERIFIED COMPANY
{company_context(company, public_url)}

COMPANIES HOUSE OFFICIAL RECORD (already retrieved by the application)
{filings_briefing}

TRACK OBJECTIVE
{track.objective}

The Companies House profile above was fetched directly by the application and may
be cited with that exact URL. Do not research or report on a similarly named entity.

Use the official record as background to direct your own research - for example to
identify a parent company or group worth researching, gauge the company's scale and
age, confirm that a web source refers to this exact entity rather than a namesake,
or spot events worth investigating such as recent director changes or newly
registered charges. Do NOT restate those official-record facts as your own
findings: the application adds them to the report separately, so repeating them
here would duplicate them. Report only what your web research adds on top."""


def specialist_structuring_instructions(track: ResearchTrack) -> str:
    return f"""You are structuring the completed {track.label.lower()} research for
an evidence-audited underwriting workflow.

Rules:
- Use only facts in RESEARCH NOTES.
- Every claim must be atomic and directly supported by one or more entries in
  AVAILABLE SOURCES.
- Copy each cited title and URL exactly from AVAILABLE SOURCES. Never invent,
  shorten, reconstruct or alter a URL.
- Omit unsupported claims and surface missing, thin or conflicting evidence in
  evidence_gaps.
- Set confidence conservatively based on source authority, independence,
  recency, corroboration and representativeness.
- The summary may synthesize only the cited claims.

Return only the requested structured findings."""


def specialist_structuring_input(
    track: ResearchTrack,
    research_notes: str,
    sources: list[dict[str, str]],
) -> str:
    return f"""Structure the {track.label.lower()} research below.

RESEARCH NOTES
{research_notes}

AVAILABLE SOURCES
{json.dumps(sources, indent=2, ensure_ascii=False)}

If AVAILABLE SOURCES is empty, return no claims and state that no cited web
evidence was captured."""


AUDITOR_INSTRUCTIONS = """You are the evidence auditor for a UK credit-underwriting
report. Three specialist agents have produced structured findings, and the
application has already removed any specialist citation whose URL was not present
in the raw web-search evidence. The business-model and quality-signals evidence
also include claims sourced directly from Companies House official registers:
officers, filing history, persons with significant control, and registered
charges. These are primary legal records, not web research, so treat them as
maximally authoritative and do not demand independent corroboration as you
would for a web claim.

Your task is to produce the only evidence that may enter the final report:
- Merge exact and near-duplicate claims, retaining all supporting source IDs.
- Detect material contradictions. If one side is clearly better supported, retain
  the better-supported claim at an appropriate confidence and record the conflict.
  If it cannot be resolved, exclude the disputed factual conclusion, record the
  contradiction, and add the uncertainty to the section's evidence gaps.
- Remove claims that are unsupported, vague, about a namesake, promotional
  generalisations, or stronger than their sources justify. Record each removal.
- Never add a fact that is absent from the specialist findings.
- Every final key point must have at least one source ID from AVAILABLE SOURCES.
- Use confidence conservatively, considering source authority, independence,
  recency, corroboration and representativeness.
- Preserve and consolidate evidence gaps. Thin or conflicting evidence must be
  explicit, especially for company quality signals.
- Section summaries may synthesise only the validated key points and must not add
  new factual propositions.
- Competitive landscape must assess degree of competition and why it matters, not
  merely list competitors.

Return only the requested structured evidence audit."""


def auditor_input(
    company: dict[str, Any],
    public_url: str,
    specialist_evidence: dict[str, Any],
    sources: list[dict[str, Any]],
) -> str:
    return f"""Audit the specialist evidence for this verified company.

VERIFIED COMPANY
{company_context(company, public_url)}

SPECIALIST EVIDENCE
{json.dumps(specialist_evidence, indent=2, ensure_ascii=False)}

AVAILABLE SOURCES
{json.dumps(sources, indent=2, ensure_ascii=False)}

Only source IDs in AVAILABLE SOURCES are valid. Produce all three final sections
plus the duplicate, contradiction and removed-claim audit trail."""
