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
# fill in COMPANIES_HOUSE_API_KEY, OPENAI_API_KEY and DATABASE_URL in .env

python scripts/load_companies.py    # one-off; see "The company name index" below
streamlit run app.py
```

Run the test suite (offline, no API calls, no database) with `pytest`.
`TEST_DATABASE_URL=postgresql://... pytest` additionally runs the candidate
generation SQL against a real Postgres - it creates and drops its own schema,
and deliberately ignores `DATABASE_URL` so it can't be pointed at the loaded
index by accident.

## The company name index

Name resolution runs against a local Postgres mirror of the [Companies House
Free Company Data Product](https://download.companieshouse.gov.uk/en_output.html),
not the Companies House search endpoint. `scripts/load_companies.py` downloads
the monthly snapshot, unpivots its `PreviousName_1..10` columns into rows, and
builds the indexes; it is build-time work, not a scheduled refresh.

**Provisioning.** The app needs `DATABASE_URL` to point at a Postgres 13+
instance where you can `CREATE EXTENSION pg_trgm` (every managed provider
allows this - it's a bundled contrib module). Any of Neon, Supabase, Railway or
Render works, as does a local `postgres` or a Docker container.

**Sizing - read this before choosing a tier.** A measured load of 525k
companies (583k name rows) came to 266 MB including indexes, which extrapolates
to **roughly 2.5-3 GB for the full ~5.5M-company register**. That does not fit
the current free tiers (Neon 0.5 GB, Supabase 0.5 GB), so a full load needs
either a paid tier or a local database. Two flags exist for working under a cap:

```bash
python scripts/load_companies.py --active-only   # skip non-active companies
python scripts/load_companies.py --limit 500000  # first N companies only
```

`--limit` truncates alphabetically rather than sampling, so a limited database
is only useful for a demo - it will silently fail to find anything past its cut
-off. `--active-only` is the better reduction of the two if you need one.

**What the snapshot does and doesn't contain.** It covers companies currently
on the register, so companies dissolved before the snapshot are absent
entirely; resolution of those depends on the registration-number path, which
goes straight to the live API. Companies House also documents its previous-name
columns as incomplete for some companies - a known, unfixed issue with the bulk
product - so former-name matching is best-effort. The snapshot is only ever
used to *find* a company; the moment one is selected, its actual details come
from the live API.

## Architecture

```
Streamlit UI (app.py)
        │
        ▼
Orchestrator  (src/orchestrator.py)
        │
   ┌────┴─────────────────────────────┐
   ▼                                  ▼
Resolver                        Research workflow
(companies_house/resolver.py)   (research/agent.py)
   │                                  │
   ├─ name_index.py → Postgres        │
   │    (pg_trgm candidate generation)│
   ├─ ranking.py    (Python rerank)   │
   │                                  │
   ▼                                  ▼
CH Client                    OpenAI Agents SDK
(companies_house/client.py)  ├─ Business-model specialist ─┐
                             ├─ Competition specialist ─────┼─ in parallel
                             └─ Quality-signals specialist ─┘
                                              │
                                              ▼
                              Raw-search citation validation
                              + SourceRegistry (validation.py)
                                              │
                                              ▼
                                  Evidence-auditor agent
                                  (dedupe / contradictions /
                                   unsupported-claim removal)
                                              │
                                              ▼
                              Deterministic report builder
                              (report/builder.py)
```

**Company resolution is deterministic, not LLM-driven.** The brief is explicit
that ambiguity must be surfaced, not silently guessed. `resolver.py` detects
whether the input looks like a CH registration number and fetches it directly;
otherwise it resolves the name against the local index and either auto-resolves
a single exact active match or returns candidates for the user to pick from in
the UI. This identity step happens entirely before any LLM call - getting the
wrong company is a worse failure mode than any amount of UI friction, and it's
not a problem an LLM is better positioned to solve than a straightforward
search+compare.

**Name matching is retrieve-wide-then-rerank, optimised for recall@10.** The
Companies House search endpoint isn't fuzzy - one mistyped letter can return
nothing at all - and every retry costs a rate-limited round trip, which capped
how much error tolerance was affordable. Against a local index that inverts:

- *Candidate generation* (`name_index.py`) is one SQL statement whose branches
  union together exact matches on the normalised name, exact matches on ~700
  one-edit variants of short queries, prefix matches, `pg_trgm` whole-string
  similarity, and `pg_trgm` word similarity for queries that match a run of
  words inside a longer name. Current and previous names sit in the same table,
  so both are searched by construction. The result is a deliberately
  indiscriminate 100-500 row shortlist; a wrong candidate costs one row of
  reranking, a missing one costs the answer.
- *Reranking* (`ranking.py`) scores that shortlist in Python with
  Damerau-Levenshtein, trigram and bigram overlap, token-set and token-sort
  similarity, exact-match bonuses, and a current-over-previous-name preference,
  then keeps the best-scoring name per company. The shortlist is small, so the
  cost of a measure is irrelevant and only its usefulness matters.

The target is the right company being *somewhere in the ten shown*, not top-1
accuracy - the user picks, so the ranking only has to put the answer in front
of them. Measured against the full loaded register (~5.7M companies, 6.4M name
rows): "aliica bank limited" ranks Allica Bank first, "astra zeneca" ranks
AstraZeneca plc first, "amzaon" ranks Amazon second - typically in well under a
second.

**Two things only showed up post-load, at full (~6.4M-row) scale - a 583k-name
subset never produced a candidate pool big enough to expose either.**

- **The whole-string trigram threshold is 0.65, not `pg_trgm`'s 0.3 default.**
  A query that shares its trigrams with a very common fragment of company
  names - a legal suffix ("monzo bank limited"), or an expanded "&" turning a
  short query into "m and s", one of the commonest word pairs in the register -
  passes a 0.3 threshold against a huge fraction of the table: "monzo bank
  limited" matched 294,209 of 6.4M rows at 0.3, several seconds dominated by
  fetching that many rows to score, not by the sort. Raised in steps against
  the loaded register, re-checking recall at each one against a battery
  including deliberately adversarial cases ("aliica bank limited", "monzo bank
  limted", "marks and spencer plcc", "M&S") - recall was unchanged from 0.3
  through 0.7, so the extra rows admitted at lower thresholds were noise the
  reranker discards anyway, not recall the system depends on.
- **The word-similarity branch (`<%`) is two `UNION` members, not one `OR`
  across columns.** Every other branch in the candidate-generation query
  already keeps its norm and stem checks in separate `UNION` members; this one
  didn't, and `EXPLAIN` on the loaded register caught the planner costing that
  combined form as a full sequential scan of the 6.4M-row table for some query
  strings (9+ seconds) instead of the bitmap index scans it used everywhere
  else. Splitting it to match the rest of the statement fixed it structurally.

**Normalisation has exactly one implementation** (`names.py`), used by both the
loader and the query path, which is why the normalised forms live in an
auxiliary table rather than in expression indexes over `company_name`. An
expression index would need that logic restated as an IMMUTABLE SQL function,
and the day the two copies drifted, names would silently stop matching queries
that should find them.

**Research is a four-agent, two-stage workflow**, once the company is fixed:

- *Stage A - three parallel specialists.* The OpenAI Agents SDK runs separate
  business-model, competitive-landscape and quality-signals agents concurrently
  with `asyncio.gather`. Each worker first uses the hosted `WebSearchTool` to
  produce cited research notes, preserving the Responses API's authoritative
  `url_citation` annotations. It then runs a schema-enforced structuring pass
  restricted to those captured URLs, producing `SpecialistFindings` with atomic
  claims, citations, confidence and evidence gaps. This two-step shape is
  deliberate: native structured-output messages do not carry web citation
  annotations. Each specialist can refine its searches until it has enough
  evidence or reaches `MAX_RESEARCH_TURNS` (default 8). A failed specialist
  becomes an explicit low-confidence evidence gap, so the other two tracks can
  still be audited.
- *Stage B - closed-registry evidence audit.* The application requests
  `web_search_call.action.sources` in the raw Responses payload. A specialist
  citation is converted to a stable integer source ID only if its URL appears
  in that raw payload (or is the already-fetched Companies House profile);
  unmatched citations and their claims are rejected before the audit. The
  evidence-auditor agent receives only this ID-backed evidence. It merges
  duplicate claims, flags unresolved contradictions, removes weak or
  unsupported claims, consolidates gaps and emits the three final structured
  sections. A final mechanical filter again rejects unknown IDs. **The report
  builder never sees the specialists' unreviewed output.**

Agents SDK trace export is disabled by default because Zero Data Retention
organizations cannot ingest traces. Non-ZDR deployments can opt in with
`OPENAI_AGENTS_TRACING_ENABLED=true`.

**Uncertainty is a first-class field, not an afterthought.** Every section and
every individual claim carries a `confidence` (high/medium/low), and every
section carries its own `evidence_gaps` - things an underwriter would want to
know that the research didn't find. The report's "Data Completeness &
Caveats" section surfaces these gaps up front rather than burying them, and
flags specialist failures, research-limit exhaustion and contradictions
explicitly.

**Report building is pure formatting - no LLM calls.** Once the audit returns,
turning it into markdown is a deterministic function of already-grounded data
(`report/builder.py`), which keeps that step trivially testable and makes it
impossible for a rendering bug to introduce new claims.

## Key design decisions

- **OpenAI Agents SDK plus hosted `WebSearchTool` instead of a dedicated search API.**
  The environment only provisioned Companies House and OpenAI keys. Using the
  Responses API's built-in search through the SDK gives each specialist real,
  inspectable source payloads without another search vendor. Python
  orchestration keeps fan-out/fan-in deterministic: all specialists run, then
  exactly one audit runs.
- **Citations are IDs into a closed registry, not free-text URLs.** This is
  the single most important grounding decision in the system - it turns
  "please don't hallucinate a source" from a prompting request into a
  structural guarantee. URL normalisation handles tracking parameters and
  fragments, but a URL still must match raw search evidence before becoming an
  ID. The auditor can cite only those IDs, and a post-audit filter enforces the
  boundary again.
- **The evidence auditor is a hard report boundary.** Specialists optimise for
  recall within narrow topics; the auditor optimises for precision across all
  three. Duplicate merging, contradiction handling and unsupported-claim
  removal therefore happen before report generation rather than as cosmetic
  caveats after it.
- **Companies House domain migration.** Companies House has migrated its
  public hosts from `*.gov.uk` to `*.company-information.service.gov.uk`
  (the old `api.company-information.gov.uk` no longer resolves as of this
  build). `src/config.py` points at the new hosts.
- **Postgres only, no search engine.** `pg_trgm` covers trigram retrieval, and
  reranking is Python over a few hundred rows, so Elasticsearch would add an
  operational dependency to do a job the database already does at this scale.
- **The LLM query suggester survives the move, in a narrower role.** It used to
  do double duty: rescue queries that Companies House's non-fuzzy search
  couldn't match at all, and offer alternatives. Retrieval now handles the
  first job, so what's left is the part no amount of edit distance can reach -
  colloquial names that simply aren't what a company is registered as
  ("spoons", "M&S"). One consequence worth naming: with a full local index
  there is always *something* that looks similar, so the weak-match threshold
  that used to trigger auto-correction now rarely fires. "Did you mean X?",
  which the user opts into, is the suggester's primary surface, and that is the
  safer of the two behaviours anyway.
- **Auto-resolve requires an unambiguous exact match, counting former names.**
  If one company is called this today and another used to be, the user is asked
  - silently picking the current one would be right most of the time, and the
  point of this step is the times it isn't.

## What was deferred, and why

- **No PDF export** - markdown covers the "citation-backed report" requirement
  and is trivially downloadable; PDF generation is pure polish for a first
  pass.
- **No caching of report data.** The database mirrors company *names*, for
  resolution only. Every report run still re-fetches the live profile and
  re-runs web research. A real deployment
  underwriting the same company repeatedly would want to cache those too.
- **No scheduled refresh of the index.** It's loaded once by hand. The snapshot
  is monthly and only used to find a company, so staleness costs recall on
  companies incorporated or renamed since the load - re-running the loader is
  the fix, and automating it wasn't worth it for a demo.
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

- **Measure recall@10 against a labelled query set** rather than the handful of
  hand-checked queries the ranking weights were tuned on. The retrieval
  branches and the blend weights are both judgement calls right now; a few
  hundred (typed query -> intended company) pairs would turn tuning them from
  an argument into an experiment, and would say what the branch limits and the
  trigram threshold actually cost.
- **Reconsider the ranking scale.** Scores are unbounded above so that bonuses
  can break ties, which makes them useful for ordering and close to useless as
  a confidence figure - a near-miss on a 5M-name index can score 0.95. A
  calibrated 0-1 score would let the UI say how sure it is, and would give the
  weak-match threshold something real to test.
- **Cache the Companies House profile and validated web evidence** - repeat
  underwriting runs for the same entity currently redo all network work.
  Time-bounded caches would improve latency while retaining explicit evidence
  timestamps.
- **Build a labelled evidence-audit evaluation set.** The auditor currently
  uses a strong schema, a closed citation registry and deterministic
  post-validation, but its duplicate and contradiction judgements are still
  qualitative. Representative claim/source bundles with expected keep,
  merge, reject and conflict decisions would make prompt and model changes
  measurable.
- **Persist partial raw search results across process failure.** An individual
  specialist failure is already surfaced as a gap while the other tracks
  continue, but a process crash or auditor API failure still loses the
  in-memory research bundle.
- **Structured SIC code descriptions.** Companies House only returns SIC
  codes, not their descriptions; mapping these to human-readable industry
  labels would make the official record more immediately useful to an
  underwriter without them having to look codes up separately.
