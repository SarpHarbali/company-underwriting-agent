# System Design Note

## Architectural approach

This is a small, modular monolith: one Python process serves the Streamlit UI
and coordinates company resolution, Companies House data retrieval, LLM
research, evidence validation, and Markdown report generation. PostgreSQL is a
supporting search index, not the system of record; live Companies House records
and public web sources remain authoritative.

```text
User
  -> Streamlit UI
  -> entity resolution
       -> registration number: live Companies House profile
       -> company name: local PostgreSQL/pg_trgm index
          -> deterministic Python ranking
          -> explicit user confirmation
          -> optional LLM query correction for weak results
  -> evidence gathering
       -> Companies House officers, filings, PSCs and charges
       -> 3 parallel web specialists:
          business model | competition | quality signals
  -> schema-constrained structuring and citation validation
  -> closed-source evidence audit
  -> deterministic Markdown report builder
```

The workflow is deliberately staged rather than implemented as one general
agent. Entity resolution must complete before research starts, each research
agent has a narrow remit, and the final auditor can use only evidence already
registered by the application. This makes failures and unsupported claims
easier to detect, test, and explain.

## Company-resolution approach

The brief suggested using the Companies House API for company search. I use the
API for exact registration-number lookups, the selected company's current
profile, and its officers, filings, persons with significant control, and
charges. I deliberately did not use its company-name search endpoint as the
main entity-resolution mechanism.

The API search works for correctly spelled names, but is not sufficiently
typo-tolerant for inputs such as misspellings, partial trading names, changed
names, or names with different legal suffixes. Compensating in the application
would require generating many spelling and name variants and issuing multiple
API searches per user query. That would add latency, consume the API rate
limit, make results dependent on a network service, and still provide less
control over recall and ranking.

Instead, a loader downloads the Companies House Free Company Data Product and
streams the compressed CSV into PostgreSQL using bulk `COPY`. The loader:

1. stores the current company name, company number, status, incorporation date,
   and postcode;
2. unpivots up to ten former-name columns so current and previous names are
   independently searchable;
3. creates normalised forms with punctuation and accents removed, plus stemmed
   forms with legal suffixes such as “Limited” removed; and
4. builds B-tree pattern indexes for exact, one-edit and prefix searches and
   GIN `pg_trgm` indexes for whole-string and word similarity.

At query time PostgreSQL produces a broad candidate set using those indexed
strategies. Python then ranks the candidates using token similarity, edit
distance, character n-gram overlap, company status, age, and whether the match
was a current or former name. This turns typo handling into one local,
deterministic query rather than a burst of external API calls.

The bulk snapshot is used only for discovery and can be refreshed by rerunning
the loader. After the user selects a candidate, the application fetches the
live Companies House profile by registration number. This hybrid approach
combines fuzzy, controllable retrieval with current authoritative company
data.

A convincing report about the wrong namesake is a serious underwriting
failure. Name searches therefore return a ranked shortlist—even for one strong
match—and require user confirmation. The LLM may suggest a spelling or
brand-name interpretation, but it cannot choose the legal entity or invent a
registration number. This keeps a probabilistic component away from the
highest-risk decision.

## Research approach

The research workflow starts only after the user has confirmed the legal
entity. Its goal is not to produce a general company summary; it is to answer
the three questions in the underwriting brief with evidence appropriate to
each one:

1. how the company operates, earns revenue, and serves customers;
2. how intense competition is in its relevant markets and why that matters;
   and
3. what external quality signals exist, how strong they are, and where the
   evidence is thin or conflicting.

### Establish a verified factual baseline

The application first retrieves structured Companies House data for the
confirmed registration number: the company profile, officers, recent filing
history, persons with significant control, and registered charges. These are
converted into evidence by deterministic code and summarised into a briefing
for the research agents.

Starting from this official baseline serves two purposes. It anchors every
agent to the correct legal entity and gives the agents useful search context,
such as company age, directors, ownership, recent corporate events, and group
relationships. It also avoids asking an LLM to rediscover structured facts that
the application can obtain directly from an authoritative source.

### Divide the brief into three research tracks

The application launches three specialist agents concurrently:

- **Business model:** products and services, revenue model, customer groups,
  routes to market, operating footprint, and material dependencies.
- **Competitive landscape:** relevant sector and geography, direct and
  indirect competitors, barriers to entry, differentiation, concentration,
  switching costs, and the resulting degree of competitive pressure.
- **Company quality signals:** customer reviews, regulatory or professional
  standing, trade coverage, awards, complaints, controversies, and the
  quantity and independence of the available evidence.

Each specialist receives the same verified company context and Companies House
briefing but a different objective and search instructions. The agents use
OpenAI's web-search tool, refine searches when results are weak, and stop when
the assigned topic is adequately covered or further searches are no longer
producing material evidence. Running the tracks in parallel reduces elapsed
time, while the separation gives each topic its own context budget and makes a
partial failure visible rather than losing the whole report.

The search strategy prefers primary and authoritative sources, followed by
reputable trade press, regulators, and established review sources.
Company-authored pages can support factual descriptions of products or
operations, but are treated as weak evidence for independent assessments of
quality. Agents are instructed to verify that each source relates to the
confirmed legal entity and to record missing, stale, ambiguous, or
contradictory evidence rather than filling gaps with general sector knowledge.

### Separate discovery from structured extraction

Each specialist first performs open-ended search and produces cited research
notes. A second, tool-free pass converts those notes into a strict Pydantic
schema containing atomic claims, source URLs, qualitative confidence, and
evidence gaps.

This two-pass design lets the research agent concentrate on discovery and query
refinement without simultaneously having to maintain a complex output schema.
The structuring pass cannot browse or add new evidence; it can only organise
what the research pass found.

### Register and validate evidence mechanically

The application extracts the URLs actually returned in the raw web-search
responses, canonicalises them, removes duplicates, and assigns stable source
IDs. A structured claim is admitted only if every cited URL maps back to this
registry. Claims containing invented, altered, or otherwise unobserved URLs
are removed before audit.

Official Companies House findings follow a separate trusted route: the
application generated them directly from API responses, registers the
corresponding official URLs, and merges them with the validated web findings.
This produces one evidence package across the three report sections.

### Audit the combined evidence

A final evidence-auditor agent receives the structured claims and only the
source IDs already registered by the application. It cannot browse or introduce
new sources. Its responsibilities are to:

- merge duplicate and overlapping claims while preserving their sources;
- remove vague, promotional, unsupported, or namesake-related claims;
- identify contradictions and either resolve them in favour of better evidence
  or record the disagreement as an evidence gap;
- assess confidence conservatively using source authority, independence,
  recency, corroboration, and representativeness; and
- ensure the competition section explains competitive intensity and its
  implications rather than merely listing competitors.

The auditor returns all three report sections plus a review trail of merged
duplicates, removed claims, and contradictions. Deterministic validation then
checks its source IDs once more and rejects anything outside the closed
registry.

### Render a bounded, explainable output

The final Markdown report is assembled by ordinary Python rather than another
generative pass. It contains the three requested sections, confidence labels,
citation IDs, evidence gaps, research warnings, audit results, and a reference
list. Because the renderer can only use the validated audit structure, it
cannot introduce uncited facts while improving the prose.

Research is bounded by configurable turn limits for each specialist and the
auditor. Reaching a limit or losing one source produces a warning or explicit
gap rather than an assumption of completeness. This is a point-in-time public
information review, not an automated credit decision: it helps an underwriter
find and assess evidence while preserving the need for human judgement.

## Technology and provider choices

- **Language — Python 3:** Python gives direct access to the OpenAI SDKs,
  Pydantic, Streamlit, PostgreSQL clients, and mature text-matching libraries.
  It also keeps orchestration, schemas, API integration, and tests in one
  language. For this prototype, development speed and legibility mattered more
  than the throughput benefits of a split frontend/backend stack.
- **UI framework — Streamlit:** Streamlit provides forms, session state,
  progress updates, candidate selection, report rendering, and downloads with
  little presentation-layer code. It is a good demo surface, but not the
  intended long-term architecture for a multi-user underwriting product.
- **Application structure — modular monolith:** Separate packages own company
  resolution, research, persistence, and rendering, while `Orchestrator`
  provides a thin application boundary. A single deployable keeps the
  prototype easy to run and debug; there is not yet enough load or independent
  lifecycle complexity to justify services or a queue.
- **Search store — PostgreSQL with `pg_trgm`:** The Companies House bulk
  dataset is indexed locally so typo-tolerant name search is fast and
  controllable. Exact, one-edit, prefix, trigram, and word-similarity retrieval
  generate candidates; RapidFuzz then ranks them. PostgreSQL was chosen over a
  dedicated search service to reduce infrastructure and because the required
  matching is well served by B-tree and trigram indexes. The live Companies
  House API remains the source of truth once a company has been selected.
- **LLM provider — OpenAI:** The implementation uses the OpenAI Agents SDK for
  specialist research and auditing, the built-in web search tool for public
  evidence, and the Responses API for query suggestions. Defaults are
  `gpt-5.6-luna` for research and query correction and `gpt-5.6-terra` for the
  final evidence audit; all role assignments are environment-configurable. The
  faster model is used for parallel, bounded tasks, while the stronger model is
  reserved for the higher-judgement consolidation step. Agent tracing is off
  by default to support Zero Data Retention-compatible deployments.
- **Contracts — Pydantic:** Specialist findings and the final audit have strict
  schemas with unknown fields forbidden. This turns malformed model output
  into a visible failure and gives validation and rendering stable inputs.

## Key design decisions

### Combine official records with independent web research

Companies House provides authoritative identity, governance, filing, control,
and charge data, but it does not adequately describe customers, revenue model,
competition, or market reputation. The system therefore treats official
records as primary evidence and runs three focused web-research tracks in
parallel for the qualitative sections. Parallelism reduces latency and narrow
prompts reduce cross-topic drift.

### Treat citation validity as an application invariant

The system does not trust citations merely because an LLM emitted them.
Sources are extracted from raw web-search responses, canonicalised, and
assigned stable IDs. A specialist claim is removed if its URL was not observed
in that raw evidence. The auditor receives a closed registry of allowed IDs,
and a final mechanical check rejects unknown IDs before rendering. Companies
House claims follow a separate trusted path because the application constructs
them directly from API responses.

This proves source provenance, not that every source is true or that every
inference is perfect. Confidence remains a qualitative assessment based on
authority, independence, recency, corroboration, and representativeness.

### Degrade visibly instead of fabricating completeness

Each specialist can fail independently; the other tracks can still complete,
while the failed track becomes an explicit evidence gap. Search budgets are
bounded, retries cover transient Companies House failures, contradictions are
reported, and weak evidence lowers confidence. The report is assembled
deterministically from audited structures so a final prose-generation pass
cannot introduce uncited facts.

## What I chose to build vs. defer, and why

| Built now | Why it was prioritised |
|---|---|
| Reliable entity disambiguation, including former names and typos | Research on the wrong legal entity invalidates everything downstream. |
| Live Companies House records plus focused public-web research | Together they cover both authoritative corporate facts and the qualitative brief. |
| Parallel specialists, strict schemas, source registry, citation checks, and evidence audit | Grounding, uncertainty, and traceability are core evaluation and underwriting risks. |
| A usable Streamlit flow with progress, review, and Markdown download | It demonstrates the complete journey with minimal non-core UI work. |
| Unit tests around matching, data access, evidence validation, parallel research, and rendering | The deterministic boundaries are where regressions can be caught cheaply. |

| Deferred | Why it was deferred |
|---|---|
| Authentication, roles, case management, and a persistent report store | Important for production, but they do not validate the core research approach. |
| Background jobs, queues, cancellation, and horizontal scaling | Current usage is prototype-scale; these add operational complexity before workload characteristics are known. |
| Research caching and scheduled refresh | Cache policy needs product decisions about acceptable staleness and auditability. |
| PDF/Word export and a richer frontend | Markdown is sufficient to evaluate report quality; presentation polish was lower priority than evidence integrity. |
| Paid/private datasets, document OCR, financial spreading, and credit decisioning | These materially expand scope, licensing, and model-risk obligations beyond public underwriting intelligence. |
| Formal confidence calibration and broad automated quality evaluation | Useful labels and adjudicated evaluation sets take time to create; uncalibrated numeric scores would imply false precision. |
| Automated selection of a “best” company match | Human confirmation is a deliberate safety control, not unfinished automation. |

## What I would do differently with more time

First, I would build evaluation before adding more agent autonomy: a labelled
company-resolution set measured with recall@10 and ranking metrics, plus an
underwriter-reviewed corpus for claim support, citation correctness,
contradiction handling, section usefulness, latency, and cost. Those results
would drive prompt, threshold, model, and search-budget changes.

Next, I would separate interactive requests from long-running research using an
API, durable job queue, worker pool, and persistent case/report store. That
would enable retries by stage, cancellation, resume, audit history, access
control, and observability without holding a Streamlit session open. I would
also add a source cache with provenance, retrieval timestamps, expiry rules,
and an explicit “refresh report” path.

Finally, I would deepen evidence coverage: retrieve and parse relevant filing
documents and accounts, map SIC codes to descriptions, record evidence dates,
apply source-specific freshness rules, and add human review/editing before a
report becomes part of a credit decision. I would keep the same central
principle: LLMs propose and synthesise within narrow boundaries; deterministic
code controls identity, provenance, validation, and final output.
