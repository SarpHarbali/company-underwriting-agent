"""Function tools exposed to the research agent, scoped to a single resolved company.

Each tool takes no arguments - the company is fixed for the whole research
session (it was already deterministically resolved before the agent ever runs),
so there's no parameter for the model to get wrong. Tool results are formatted
as plain text summaries for the model to read, and every successful call
registers a Companies House page in the shared SourceRegistry so it's citable.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from src.companies_house.client import CompaniesHouseClient, CompanyNotFoundError, RateLimitedError
from src.research.sources import SourceRegistry

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "get_filing_history",
        "description": (
            "Get the company's recent Companies House filing history (accounts, "
            "confirmation statements, capital changes, officer changes, etc.), "
            "most recent first. Useful for understanding company trajectory and "
            "recency/quality of financial reporting."
        ),
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "strict": True,
    },
    {
        "type": "function",
        "name": "get_officers",
        "description": (
            "Get current and past directors/officers of the company, with appointment "
            "and resignation dates. Useful for understanding leadership stability."
        ),
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "strict": True,
    },
    {
        "type": "function",
        "name": "get_persons_with_significant_control",
        "description": (
            "Get persons/entities with significant control (PSC) of the company - i.e. "
            "who actually owns/controls it. Useful for ownership structure context."
        ),
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "strict": True,
    },
    {
        "type": "function",
        "name": "get_charges",
        "description": (
            "Get registered charges (mortgages/debentures) against the company's assets. "
            "Useful signal on secured lending/financial structure."
        ),
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "strict": True,
    },
]


def _summarise_filing_history(items: list[dict[str, Any]]) -> str:
    if not items:
        return "No filing history returned."
    lines = []
    for item in items[:20]:
        lines.append(
            f"- {item.get('date', '?')} | {item.get('type', '?')} | "
            f"{item.get('description', item.get('category', ''))}"
        )
    return "\n".join(lines)


def _summarise_officers(items: list[dict[str, Any]]) -> str:
    if not items:
        return "No officers returned."
    lines = []
    for item in items[:20]:
        status = "resigned" if item.get("resigned_on") else "active"
        lines.append(
            f"- {item.get('name', '?')} | role: {item.get('officer_role', '?')} | "
            f"appointed: {item.get('appointed_on', '?')} | status: {status}"
            + (f" (resigned {item.get('resigned_on')})" if item.get("resigned_on") else "")
        )
    return "\n".join(lines)


def _summarise_psc(items: list[dict[str, Any]]) -> str:
    if not items:
        return "No persons with significant control returned (may be exempt or none registered)."
    lines = []
    for item in items[:20]:
        kind = item.get("kind", "?")
        name = item.get("name", "unknown")
        natures = ", ".join(item.get("natures_of_control", []) or [])
        lines.append(f"- {name} | kind: {kind} | natures of control: {natures}")
    return "\n".join(lines)


def _summarise_charges(items: list[dict[str, Any]]) -> str:
    if not items:
        return "No registered charges returned."
    lines = []
    for item in items[:20]:
        lines.append(
            f"- status: {item.get('status', '?')} | created: {item.get('created_on', '?')} | "
            f"type: {item.get('classification', {}).get('description', '?')}"
        )
    return "\n".join(lines)


def build_tool_dispatch(
    client: CompaniesHouseClient,
    company_number: str,
    registry: SourceRegistry,
    company_title: str,
) -> dict[str, Callable[[], str]]:
    public_url = client.public_company_url(company_number)

    def _safe(kind: str, source_suffix: str, source_label: str, fetch, summarise) -> str:
        try:
            items = fetch(company_number)
        except CompanyNotFoundError:
            return f"Companies House has no {kind} data for this company."
        except RateLimitedError as exc:
            return f"Companies House lookup for {kind} failed (rate limited/unavailable): {exc}"
        url = f"{public_url}{source_suffix}"
        registry.add("companies_house", f"{company_title} - {source_label}", url)
        return summarise(items)

    return {
        "get_filing_history": lambda: _safe(
            "filing history", "/filing-history", "Filing history",
            client.get_filing_history, _summarise_filing_history,
        ),
        "get_officers": lambda: _safe(
            "officers", "/officers", "Officers", client.get_officers, _summarise_officers,
        ),
        "get_persons_with_significant_control": lambda: _safe(
            "PSC", "/persons-with-significant-control", "Persons with significant control",
            client.get_persons_with_significant_control, _summarise_psc,
        ),
        "get_charges": lambda: _safe(
            "charges", "/charges", "Charges", client.get_charges, _summarise_charges,
        ),
    }


def call_tool(dispatch: dict[str, Callable[[], str]], name: str, arguments_json: str) -> str:
    fn = dispatch.get(name)
    if fn is None:
        return json.dumps({"error": f"Unknown tool '{name}'"})
    return fn()
