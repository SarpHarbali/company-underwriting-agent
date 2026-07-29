from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from src.companies_house.client import (
    CompaniesHouseAPIError,
    CompaniesHouseClient,
    CompanyNotFoundError,
    RateLimitedError,
)
from src.research.schemas import (
    Confidence,
    SourceCitation,
    SpecialistClaim,
    SpecialistFindings,
)

_RECENT_WINDOW = timedelta(days=365)
_STALE_FILING_WINDOW = timedelta(days=548)  # ~18 months


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _officer_claims(officers: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    if not officers:
        return [], ["No officer records were found for this company."]

    active = [officer for officer in officers if not officer.get("resigned_on")]
    resigned = [officer for officer in officers if officer.get("resigned_on")]
    claims: list[str] = []

    if active:
        names = "; ".join(
            f"{officer.get('name', '?')} "
            f"({officer.get('officer_role', 'officer')}, "
            f"appointed {officer.get('appointed_on', 'unknown date')})"
            for officer in active[:10]
        )
        claims.append(f"{len(active)} active officer(s) on record: {names}.")
    else:
        claims.append("No active officers are currently on record.")

    cutoff = date.today() - _RECENT_WINDOW
    recent_resignations = [
        officer
        for officer in resigned
        if (_parse_date(officer.get("resigned_on")) or date.min) >= cutoff
    ]
    if recent_resignations:
        names = "; ".join(
            f"{officer.get('name', '?')} "
            f"(resigned {officer.get('resigned_on')})"
            for officer in recent_resignations[:10]
        )
        claims.append(
            f"{len(recent_resignations)} officer(s) resigned in the past 12 months: {names}."
        )
    return claims, []


def _filing_history_claims(filings: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    if not filings:
        return [], ["No Companies House filing history was found for this company."]

    most_recent = filings[0]
    claims = [
        f"Most recent Companies House filing: {most_recent.get('date', 'unknown date')} "
        f"({most_recent.get('description') or most_recent.get('type', 'unspecified type')})."
    ]
    cutoff = date.today() - _RECENT_WINDOW
    recent_count = sum(
        1
        for filing in filings
        if (_parse_date(filing.get("date")) or date.min) >= cutoff
    )
    claims.append(f"{recent_count} filing(s) recorded with Companies House in the last 12 months.")

    gaps: list[str] = []
    most_recent_date = _parse_date(most_recent.get("date"))
    if most_recent_date and most_recent_date < date.today() - _STALE_FILING_WINDOW:
        gaps.append(
            f"Most recent Companies House filing is from {most_recent_date.isoformat()}, "
            "over 18 months old."
        )
    return claims, gaps


def _psc_claims(controllers: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    if not controllers:
        return [], [
            "No persons with significant control are registered "
            "(the company may be exempt or this may not yet be filed)."
        ]
    descriptions = []
    for controller in controllers[:10]:
        name = controller.get("name", controller.get("kind", "unknown"))
        control = (
            ", ".join(controller.get("natures_of_control", []) or [])
            or "nature of control not specified"
        )
        descriptions.append(f"{name} ({control})")
    names = "; ".join(descriptions)
    return [
        f"{len(controllers)} person(s)/entities with significant control "
        f"registered: {names}."
    ], []


def _charges_claims(charges: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    if not charges:
        return ["No registered charges were found against the company's assets."], []
    outstanding = [
        charge
        for charge in charges
        if charge.get("status") not in {"satisfied", "fully-satisfied"}
    ]
    return [
        f"{len(charges)} registered charge(s) found, {len(outstanding)} outstanding."
    ], []


BUSINESS_MODEL = "business_model"
QUALITY_SIGNALS = "quality_signals"

_SECTION_SUMMARIES = {
    BUSINESS_MODEL: "Companies House ownership and control records.",
    QUALITY_SIGNALS: "Companies House officer, filing history and charge records.",
}


def _build_section_findings(
    section: str,
    claims: list[SpecialistClaim],
    gaps: list[str],
) -> SpecialistFindings:
    return SpecialistFindings(
        summary=(
            _SECTION_SUMMARIES[section]
            if claims
            else f"No {_SECTION_SUMMARIES[section][:-1].lower()} were available."
        ),
        claims=claims,
        confidence=Confidence.high if claims else Confidence.low,
        evidence_gaps=gaps,
    )


def empty_filings_evidence(reason: str) -> dict[str, SpecialistFindings]:
    return {
        section: _build_section_findings(section, [], [reason])
        for section in _SECTION_SUMMARIES
    }


def gather_filings_findings(
    ch_client: CompaniesHouseClient,
    company_number: str,
    public_url: str,
) -> dict[str, SpecialistFindings]:
    claims: dict[str, list[SpecialistClaim]] = {
        section: [] for section in _SECTION_SUMMARIES
    }
    gaps: dict[str, list[str]] = {section: [] for section in _SECTION_SUMMARIES}

    registers = (
        (
            QUALITY_SIGNALS,
            "Officers",
            f"{public_url}/officers",
            ch_client.get_officers,
            _officer_claims,
        ),
        (
            QUALITY_SIGNALS,
            "Filing history",
            f"{public_url}/filing-history",
            ch_client.get_filing_history,
            _filing_history_claims,
        ),
        (
            BUSINESS_MODEL,
            "Persons with significant control",
            f"{public_url}/persons-with-significant-control",
            ch_client.get_persons_with_significant_control,
            _psc_claims,
        ),
        (
            QUALITY_SIGNALS,
            "Charges",
            f"{public_url}/charges",
            ch_client.get_charges,
            _charges_claims,
        ),
    )

    for section, label, url, fetch_register, extract_claims in registers:
        try:
            records = fetch_register(company_number)
        except (CompanyNotFoundError, RateLimitedError, CompaniesHouseAPIError) as exc:
            gaps[section].append(
                f"{label} could not be retrieved from Companies House: {exc}"
            )
            continue
        register_claims, register_gaps = extract_claims(records)
        for claim_text in register_claims:
            claims[section].append(
                SpecialistClaim(
                    claim=claim_text,
                    citations=[
                        SourceCitation(
                            title=f"Companies House - {label}",
                            url=url,
                        )
                    ],
                    confidence=Confidence.high,
                )
            )
        gaps[section].extend(register_gaps)

    return {
        section: _build_section_findings(section, claims[section], gaps[section])
        for section in _SECTION_SUMMARIES
    }


def filings_briefing(evidence: dict[str, SpecialistFindings]) -> str:
    lines: list[str] = []
    for section in _SECTION_SUMMARIES:
        findings = evidence.get(section)
        if findings is None:
            continue
        lines.extend(f"- {claim.claim}" for claim in findings.claims)
        lines.extend(f"- Not on record: {gap}" for gap in findings.evidence_gaps)
    if not lines:
        return "- No Companies House officer, filing, ownership or charge records were retrieved."
    return "\n".join(lines)
