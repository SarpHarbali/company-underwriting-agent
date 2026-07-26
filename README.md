# Underwriting Intelligence Agent

An agentic system that turns a UK company name or Companies House number into a
citation-backed underwriting intelligence report: business model, competitive
landscape, and company quality signals.

## Running it

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# fill in COMPANIES_HOUSE_API_KEY and OPENAI_API_KEY in .env

streamlit run app.py
```

Run the test suite (offline, no API calls) with `pytest`.

## Architecture

```
Streamlit UI (app.py)
        │
        ▼
Orchestrator  (src/orchestrator.py)
        │
   ┌────┴─────────────────────────────┐
   ▼                                  ▼
Resolver                        Research Agent
(companies_house/resolver.py)   (research/agent.py)
   │                                  │
   ▼                                  ▼
CH Client                    OpenAI Responses API
(companies_house/client.py)  ├─ function tools → CH Client
                              └─ hosted web_search tool
                                       │
                                       ▼
                              SourceRegistry (research/sources.py)
                                       │
                                       ▼
                              Structured synthesis (schemas.py)
                                       │
                                       ▼
                              Report builder (report/builder.py)
```

**Company resolution is deterministic, not LLM-driven.** The brief is explicit
that ambiguity must be surfaced, not silently guessed. `resolver.py` detects
whether the input looks like a CH registration number and fetches it directly;
otherwise it searches Companies House and either auto-resolves a single exact
active match or returns candidates for the user to pick from in the UI. This
identity step happens entirely before any LLM call - getting the wrong company
is a worse failure mode than any amount of UI friction, and it's not a problem
an LLM is better positioned to solve than a straightforward search+compare.

**Research is a genuinely agentic, two-phase process**, once the company is
fixed:

- *Phase A - tool-calling research loop.* The model is given four Companies
  House function tools (filing history, officers, PSC, charges - each scoped
  to the already-resolved company, so there's no company-number parameter for
  it to get wrong) plus OpenAI's hosted `web_search` tool, and is prompted to
  decide for itself what's worth looking up for the three report sections, in
  what order, and when it has gathered enough (see `research/prompts.py`). The
  loop runs until the model stops calling tools or hits a configurable
  tool-call budget (`MAX_RESEARCH_TOOL_CALLS`, default 12) - this is where "how
  much evidence is enough" and "when to stop gathering" get handled, rather
  than hardcoding a fixed fetch sequence.
- *Phase B - constrained structured synthesis.* Every source the agent
  actually touched (a Companies House page it queried, or a URL the
  `web_search` tool returned - captured via the Responses API's
  `url_citation` annotations) is logged into a `SourceRegistry` with a stable
  integer ID. A second, separate call
  (`client.responses.parse(text_format=StructuredReport)`) turns the research
  transcript into the three report sections, and is instructed to cite *only*
  IDs from that registry. As a second line of defence, `agent.py` strips any
  cited ID that isn't actually in the registry before the report is rendered.
  **This is the main grounding mechanism**: a citation that appears in the
  final report is structurally guaranteed to trace back to a real source the
  agent saw, not a model-invented one.

**Uncertainty is a first-class field, not an afterthought.** Every section and
every individual claim carries a `confidence` (high/medium/low), and every
section carries its own `evidence_gaps` - things an underwriter would want to
know that the research didn't find. The report's "Data Completeness &
Caveats" section surfaces these gaps up front rather than burying them, and
flags explicitly if the tool-call budget was exhausted before the agent judged
itself done.

**Report building is pure formatting - no LLM calls.** Once Phase B returns,
turning it into markdown is a deterministic function of already-grounded data
(`report/builder.py`), which keeps that step trivially testable and makes it
impossible for a rendering bug to introduce new claims.

## Key design decisions

- **OpenAI's hosted `web_search` tool instead of a dedicated search API.**
  The environment only provisioned Companies House and OpenAI keys. Using the
  Responses API's built-in web search means the agent gets real, cited web
  results without a third dependency, at the cost of not being able to choose
  a specific search backend or tune ranking. Verified empirically (see git
  history / dev notes) that both `web_search` and `web_search_preview` tool
  types work with this key on `gpt-4.1`, and that hosted `web_search` and
  custom function tools can be freely mixed in one Responses API loop.
- **Function tools take no company-number argument.** Since the company is
  fixed for the whole research session, letting the model pass a
  `company_number` string is a pure liability (a typo or hallucinated number
  would silently pull the wrong company's filings). The tools are closures
  bound to the resolved company instead.
- **Citations are IDs into a closed registry, not free-text URLs.** This is
  the single most important grounding decision in the system - it turns
  "please don't hallucinate a source" from a prompting request into a
  structural guarantee, enforced twice (schema instruction + post-hoc filter).
- **Companies House domain migration.** Companies House has migrated its
  public hosts from `*.gov.uk` to `*.company-information.service.gov.uk`
  (the old `api.company-information.gov.uk` no longer resolves as of this
  build). `src/config.py` points at the new hosts.

## What was deferred, and why

- **No PDF export** - markdown covers the "citation-backed report" requirement
  and is trivially downloadable; PDF generation is pure polish for a first
  pass.
- **No persistent cache/database.** Every report run re-fetches Companies
  House and re-runs web research. Fine for a demo; a real deployment
  underwriting the same company repeatedly would want to cache CH data (it
  changes rarely) and possibly recent research (it doesn't need to be
  re-derived every session).
- **No multi-user auth/session isolation** - this is a single-user local demo,
  not a multi-tenant service.
- **Confidence is a heuristic**, not a calibrated statistical measure - it's
  the model's own judgement plus source count, which is honest about being a
  qualitative signal rather than something an underwriter should treat as a
  hard number.
- **Actual cloud deployment** was intentionally left out of this pass (agreed
  with the requester) - the app is deploy-ready (env-var driven config, no
  hardcoded local paths) but pushing it to e.g. Streamlit Community Cloud is a
  short follow-up rather than something done here.

## What I'd do differently with more time

- **Cache Companies House lookups** (profile, officers, filings) - they
  change infrequently and are the cheapest, most reliable data in the system;
  caching would speed up repeat runs and cut API calls.
- **Let the agent re-query with refined search terms.** Right now a single
  `web_search` tool definition is offered per call; a smarter loop could let
  the model see its own search's result quality and decide to re-query with a
  narrower/broader query rather than accepting whatever the first pass
  returns.
- **Add a lightweight "self-critique" pass** before finalising the report:
  a second model call that checks the drafted sections against the
  transcript specifically looking for unsupported generalisations, separate
  from the citation-validity check that already exists.
- **Surface partial results on failure.** If Companies House is down or the
  OpenAI call errors mid-loop, the current behaviour is a clean error message;
  a more resilient version would return whatever was gathered so far with a
  clear "incomplete" flag rather than nothing at all.
- **Structured SIC code descriptions.** Companies House only returns SIC
  codes, not their descriptions; mapping these to human-readable industry
  labels would make the official record more immediately useful to an
  underwriter without them having to look codes up separately.
