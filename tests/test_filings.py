from datetime import date
from types import SimpleNamespace

from src.companies_house.client import RateLimitedError
from src.research.filings import (
    BUSINESS_MODEL,
    QUALITY_SIGNALS,
    filings_briefing,
    gather_filings_findings,
)
from src.research.prompts import RESEARCH_TRACKS, specialist_input
from src.research.schemas import Confidence

PUBLIC_URL = "https://find-and-update.company-information.service.gov.uk/company/123"

PSC = [
    {
        "name": "Acme Holdings Ltd",
        "natures_of_control": ["ownership-of-shares-75-to-100-percent"],
    }
]


def _client(**overrides):
    defaults = dict(
        get_officers=lambda number: [],
        get_filing_history=lambda number: [],
        get_persons_with_significant_control=lambda number: [],
        get_charges=lambda number: [],
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _claims(evidence, section):
    return [claim.claim for claim in evidence[section].claims]


def test_ownership_goes_to_business_model_and_the_rest_to_quality_signals():
    evidence = gather_filings_findings(
        _client(
            get_persons_with_significant_control=lambda number: PSC,
            get_officers=lambda number: [
                {"name": "Jane Doe", "officer_role": "director", "appointed_on": "2019-04-02"}
            ],
            get_charges=lambda number: [{"status": "outstanding"}],
        ),
        "123",
        PUBLIC_URL,
    )

    business_model = _claims(evidence, BUSINESS_MODEL)
    quality = _claims(evidence, QUALITY_SIGNALS)

    assert any("Acme Holdings Ltd" in claim for claim in business_model)
    assert not any("Acme Holdings Ltd" in claim for claim in quality)
    assert any("active officer" in claim for claim in quality)
    assert any("registered charge" in claim for claim in quality)
    assert not any("officer" in claim for claim in business_model)
    citation = evidence[BUSINESS_MODEL].claims[0].citations[0]
    assert citation.url == f"{PUBLIC_URL}/persons-with-significant-control"


def test_empty_registers_report_gaps_and_absence_of_charges_claim():
    evidence = gather_filings_findings(_client(), "123", PUBLIC_URL)

    assert _claims(evidence, QUALITY_SIGNALS) == [
        "No registered charges were found against the company's assets."
    ]
    assert evidence[QUALITY_SIGNALS].confidence is Confidence.high
    assert _claims(evidence, BUSINESS_MODEL) == []
    assert evidence[BUSINESS_MODEL].confidence is Confidence.low

    quality_gaps = " ".join(evidence[QUALITY_SIGNALS].evidence_gaps).lower()
    assert "officer" in quality_gaps
    assert "filing history" in quality_gaps
    assert "significant control" in " ".join(evidence[BUSINESS_MODEL].evidence_gaps).lower()


def test_officer_findings_separate_active_and_recently_resigned_officers():
    officers = [
        {"name": "Jane Doe", "officer_role": "director", "appointed_on": "2020-01-01"},
        {
            "name": "John Smith",
            "officer_role": "director",
            "appointed_on": "2019-01-01",
            "resigned_on": date.today().isoformat(),
        },
    ]
    evidence = gather_filings_findings(
        _client(get_officers=lambda number: officers),
        "123",
        PUBLIC_URL,
    )
    officer_claims = _claims(evidence, QUALITY_SIGNALS)
    assert any("1 active officer" in claim for claim in officer_claims)
    assert any("1 officer(s) resigned" in claim for claim in officer_claims)


def test_charges_claim_counts_outstanding():
    charges = [{"status": "outstanding"}, {"status": "satisfied"}]
    evidence = gather_filings_findings(
        _client(get_charges=lambda number: charges),
        "123",
        PUBLIC_URL,
    )

    claim = next(c for c in evidence[QUALITY_SIGNALS].claims if "charge" in c.claim.lower())
    assert "2 registered charge(s) found, 1 outstanding" in claim.claim
    assert claim.citations[0].url == f"{PUBLIC_URL}/charges"


def test_briefing_shows_every_specialist_the_whole_record_including_ownership():
    evidence = gather_filings_findings(
        _client(get_persons_with_significant_control=lambda number: PSC), "123", PUBLIC_URL
    )
    briefing = filings_briefing(evidence)

    assert "Acme Holdings Ltd" in briefing

    prompt = specialist_input(
        RESEARCH_TRACKS[0],
        {"company_name": "Acme Ltd", "company_number": "123"},
        PUBLIC_URL,
        briefing,
    )
    assert "Acme Holdings Ltd" in prompt
    assert "Do NOT restate" in prompt


def test_fetch_error_is_surfaced_as_a_gap_in_that_registers_section():
    def boom(number):
        raise RateLimitedError("companies house is down")

    evidence = gather_filings_findings(
        _client(get_persons_with_significant_control=boom), "123", PUBLIC_URL
    )

    gaps = evidence[BUSINESS_MODEL].evidence_gaps
    assert any(
        "Persons with significant control" in gap
        and "could not be retrieved" in gap
        for gap in gaps
    )
    assert not any(
        "could not be retrieved" in gap
        for gap in evidence[QUALITY_SIGNALS].evidence_gaps
    )
