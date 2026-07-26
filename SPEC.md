## The Brief

Build an agentic system that generates an underwriting intelligence report for a UK company.

A user should be able to provide either a company name or a Companies House registration number as input. The system should handle ambiguity gracefully — if a name matches multiple entities, it should surface the right one (or ask the user to disambiguate) rather than silently proceeding with the wrong company.

The system should then autonomously gather relevant public data, extract structured evidence, and synthesise it into a clear, citation-backed report.

## Required Output

The final report must include:

1. Business model summary — What does this company actually do? How does it make money? Who are its customers?
2. Competitive landscape — A structured assessment of the competitive intensity this business faces, specific to its sector and relevant geographies. This should go beyond a list of competitors — it should offer a view on degree of competition and why that matters.
3. Company quality signals — A structured synthesis of quality indicators drawn from customer reviews, trade press, industry reputation, or similar public sources. Identify signal strength (e.g. is there enough data to be confident?) and note where evidence is thin or conflicting.

Each section should be explainable and grounded — claims must reference the sources they came from. The system should surface uncertainty rather than paper over it.

## What We're Evaluating

| Dimension | What good looks like |
|---|---|
| Agent design | Thoughtful orchestration — clear reasoning about which tools/sources to use, when to stop gathering, how to handle failures |
| Data grounding | Claims are traceable to sources; the system doesn't hallucinate or over-generalise |
| Uncertainty handling | The system knows what it doesn't know and says so clearly |
| Product thinking | The report format and UX are shaped by what an underwriter actually needs, not just what's easy to generate |
| Code quality | Readable, reasonably structured, with a clear explanation of design choices |
| Judgement on scope | what did you choose to build vs. defer, and why? |

## Deliverables

Please share:

- A working demo — ideally deployed so we can explore it with real inputs (a simple hosted URL is fine)
- Source code — we'll want to read it
- A brief system design note — covering your architectural approach, key design decisions, and what you'd do differently with more time
