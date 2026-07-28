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
Resolver                        Research Agent
(companies_house/resolver.py)   (research/agent.py)
   │                                  │
   ├─ name_index.py → Postgres        │
   │    (pg_trgm candidate generation)│
   ├─ ranking.py    (Python rerank)   │
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
  resolution only. Every report run still re-fetches profile, officers, filings
  and charges from the live API and re-runs web research. A real deployment
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
