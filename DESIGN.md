# System Design Note

## What this is

A Streamlit app that takes a UK company name or Companies House registration
number, resolves it to a single confirmed legal entity, and produces a
citation-backed underwriting report: business model, competitive landscape,
and company quality signals. Every claim in the report traces back to a
source; unsupported claims are mechanically discarded rather than trusted to
the model's honesty.

## Architectural approach

The system is split into two pipelines that share nothing but the resolved
company profile: **resolution** (turn ambiguous user input into one verified
entity) and **research** (turn a verified entity into a grounded report).
Keeping them separate matters because they fail differently — resolution
needs to be fast, deterministic and cheap to get exactly right, since the
consequence of getting it wrong is a well-written report about the wrong
company; research is inherently probabilistic and needs different failure
handling (partial results, evidence gaps, contradiction tracking).

```text
Streamlit UI
    |
    +-- Company resolver
    |     +-- local PostgreSQL name index (candidate generation)
    |     +-- Python candidate ranking (deterministic scoring)
    |     `-- live Companies House profile, fetched only after user confirms
    |
    `-- Research workflow
          +-- Companies House officers/filings/PSC/charges (fetched directly)
          +-- three parallel web-research specialists (OpenAI Agents SDK)
          +-- mechanical citation validation against raw search responses
          +-- evidence auditor (closed-registry LLM pass)
          `-- deterministic Markdown report builder
```

### Resolution: local name index instead of the Companies House search API

**Problem:** the Companies House `/search/companies` endpoint does
substring/prefix matching on the registered name, not fuzzy matching. A
misspelling, abbreviation, trading name, or missing legal suffix routinely
returns nothing or the wrong company first, with no ranked "did you mean."
Compensating client-side — generating spelling variants and querying the
endpoint for each — turns one search into many API calls against a
600-requests/5-minutes-per-key limit, and still ranks poorly, since the
endpoint has no similarity scoring of its own: the "best match" is whichever
variant happened to return a hit.

**Design decision:** load the Companies House Free Company Data Product (the
bulk CSV snapshot of every company on the register, including former names)
into a local PostgreSQL database once via `scripts/load_companies.py`, using
the `pg_trgm` extension. Name search becomes a single indexed SQL query —
trigram similarity, word similarity, and prefix/edit-distance operators run
server-side against an index (`src/db/schema.sql`, `src/companies_house/name_index.py`)
— returning a candidate set in one round trip regardless of misspelling, with
zero Companies House API calls spent on search. Python then reranks that
shortlist deterministically (token similarity, edit distance, n-gram
overlap, company status, age, current-vs-former-name preference — see
`src/companies_house/ranking.py`).

Registration-number lookups and all detail fetches (profile, officers,
filings, PSC, charges) still go directly to the live Companies House API;
only fuzzy name search is served from the local index.

**Trade-off:** this adds infrastructure (Postgres) and a data-freshness
constraint — the index is only as current as the last loader run. Fuzzy
matching is not delegated to an LLM: it is a deterministic, testable problem
against a few million rows, and an LLM would add cost and latency without
improving it. An LLM query suggester is used only as a fallback when the
deterministic match is weak (e.g. `"spoons"` → `"wetherspoon"`); its output
is just another query into the same deterministic ranker, not a resolution
decision.

Every name search surfaces candidates for the user to confirm — including
when there's a single exact match — because generating a polished,
well-cited report about the wrong legal entity is a worse failure mode than
one extra click.

### Research: parallel specialists, closed-registry audit

Once the user confirms a company, official Companies House registers
(officers, filing history, PSC, charges) are fetched directly and converted
to evidence deterministically — no LLM involved, since these are structured
records, not something that needs interpreting. That evidence is handed as
context to three parallel research agents (business model, competitive
landscape, quality signals), each using the OpenAI Agents SDK with a
`WebSearchTool`. Running them concurrently rather than as one broad agent
keeps each one's context and instructions focused on a single question, which
produces more thorough and consistent coverage per section for roughly the
same total latency as one sequential agent.

Grounding is enforced at two layers, not just prompted for:

1. **Mechanical citation validation** (`src/research/validation.py`) — a
   specialist's claim survives only if its cited URL literally appears in
   that agent's raw web-search response. This runs in code, not as an LLM
   instruction, because "cite your sources" is not a reliable way to stop a
   model from citing a plausible-looking URL it never actually retrieved.
2. **Evidence audit** — a separate LLM pass receives *only* the
   already-validated claims and their registered source IDs (a "closed
   registry"), and is responsible for merging duplicates, resolving or
   flagging contradictions, and dropping claims that are vague, promotional,
   or about a namesake. Its output then goes through the same mechanical
   check again: any source ID the auditor didn't preserve from the allowed
   set is stripped before rendering. The auditor can delete evidence; it
   cannot introduce a source that was never actually retrieved.

Confidence is deliberately left qualitative (`high`/`medium`/`low`) rather
than a numeric score, because nothing in this pipeline produces a
statistically calibrated probability — presenting one would manufacture false
precision for an underwriter who might reasonably treat "73%" as meaningful.

Failures are contained per-section rather than failing the whole report: if a
specialist agent throws, or Companies House lookups fail, that section is
marked as an evidence gap and the rest of the report still generates. An
underwriter working from a report with an honest gap in one section is much
better served than one blocked entirely by a transient tool failure in
another.

## Language, framework and provider choices

- **Python** — the natural fit given the OpenAI Agents SDK, Companies House
  tooling, and Postgres client libraries all being Python-first; also keeps
  resolution and research in one codebase and one type system (Pydantic)
  rather than splitting concerns across a backend and a script layer.
- **Streamlit** — the brief asked for a working, explorable demo, not a
  production frontend. Streamlit gets confirm/select/regenerate UX with
  session state in a few hundred lines, which left the time budget for the
  grounding pipeline instead of a hand-rolled frontend.
- **PostgreSQL + `pg_trgm`** — chosen specifically for trigram and
  word-similarity operators that make fuzzy name search fast without a
  separate search service. This is the one piece of non-trivial
  infrastructure the project takes on, and it's justified by a concrete
  requirement (typo-tolerant name search) that the alternative — the public
  Companies House search endpoint — doesn't meet.
- **OpenAI (Agents SDK, `gpt-5.6-luna` for specialists/query correction,
  `gpt-5.6-luna` variant `gpt-5.6-terra` for the auditor)** — the Agents SDK
  provides `WebSearchTool` with raw response objects that expose which URLs
  were actually retrieved, which the citation-validation layer depends on;
  swapping providers would mean re-establishing that same raw-evidence
  guarantee elsewhere. A cheaper/faster model runs the three parallel
  specialists and the low-stakes query correction; a stronger model is
  reserved for the single auditor pass, since that's the one place a mistake
  (keeping a bad claim, missing a contradiction) propagates directly into the
  final report. Both are overridable via environment variables without a
  code change. Agent tracing is off by default for Zero Data Retention
  compatibility, since this is underwriting data.

## What I chose to build vs. defer, and why

**Built:**
- Deterministic, tested name resolution with mandatory user confirmation —
  this is the highest-consequence failure mode (wrong company, confident
  report) and the cheapest to get right with plain code, so it got the most
  investment relative to its apparent size.
- Two-layer grounding (mechanical URL check + closed-registry audit) instead
  of a single "cite your sources" prompt — this is the crux of the brief
  ("claims are traceable... doesn't hallucinate") and prompting alone doesn't
  reliably deliver it.
- Explicit evidence-gap and contradiction surfacing throughout the schema,
  not just in prose — the brief asks the system to "know what it doesn't
  know," which only holds up if gaps are a structured field the report
  builder can't silently drop, not a sentence the model might omit.
- Partial-failure handling per research track and per official register, so
  one failing lookup doesn't take down the whole report.
- A real test suite around the parts most likely to silently regress:
  ranking, resolver, citation validation, filings-to-evidence conversion,
  report rendering.

**Deferred, and why:**
- **Evidence caching.** Every report re-runs all web research from scratch.
  Correct behaviour first, then caching — caching an ungrounded pipeline
  just makes wrong answers cheaper to repeat. Time-bounded caching keyed on
  company number is the natural next step now that grounding is solid.
- **PDF export.** Markdown download covers the "explainable, shareable
  report" requirement; a PDF renderer is pure polish with no bearing on the
  dimensions being evaluated (grounding, uncertainty handling, agent
  design).
- **Multi-user auth / persistent report store.** Out of scope for a
  single-reviewer demo; adding it would mean building account and storage
  infrastructure instead of the grounding logic the brief actually weighs.
- **A labelled evaluation set for name ranking and evidence-audit
  decisions.** The ranking weights and audit prompt are currently tuned by
  hand against observed cases (documented inline in `ranking.py` and the
  README) rather than against a scored dataset. This is the biggest thing
  I'd change with more time — see below.
- **LLM-driven interpretation of official Companies House data.** Officer,
  filing, PSC and charge records are converted to evidence with plain code,
  not summarised by a model. These are structured, unambiguous records; an
  LLM pass would add latency and hallucination risk for no interpretive
  value.

## What I'd do differently with more time

1. **Build a labelled eval set.** Recall@10 for name ranking against a set of
   real (query → correct company) pairs, and a scored set of evidence-audit
   decisions (should this claim survive, should this contradiction be
   flagged). Right now correctness rests on code review and spot-checking;
   an eval set would catch silent regressions in ranking weights or prompt
   changes that manual testing won't.
2. **Time-bounded evidence caching**, keyed on company number, so repeated
   reports on the same company don't repeat every web search — both a cost
   and latency win once correctness is established.
3. **SIC code descriptions.** The official record currently exposes raw SIC
   codes without their text descriptions, which are more useful to an
   underwriter and to the research specialists' sector-scoping.
4. **A confidence rubric visible to the underwriter**, not just the model —
   right now "high/medium/low" is defined only in the prompt; surfacing the
   actual criteria in the UI would make the qualitative judgement easier for
   an underwriter to calibrate against and challenge.
5. **Deploy behind a lightweight auth gate** so the hosted demo isn't fully
   open, and add basic rate limiting given each report triggers real paid
   API calls (Companies House + multiple OpenAI agent runs).
