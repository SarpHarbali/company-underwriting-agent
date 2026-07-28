# Underwriting Intelligence Agent

A Streamlit application that resolves a UK company and produces a
citation-backed underwriting report covering its business model, competitive
landscape, and company quality signals.

## Running locally

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Set COMPANIES_HOUSE_API_KEY, OPENAI_API_KEY, and DATABASE_URL.

python scripts/load_companies.py
streamlit run app.py
```

`DATABASE_URL` must point to PostgreSQL 13+ with permission to create the
bundled `pg_trgm` extension. The loader accepts an existing archive and can
build a smaller development index:

```bash
python scripts/load_companies.py --zip path/to/company-data.zip
python scripts/load_companies.py --active-only
python scripts/load_companies.py --limit 500000
```

The full Companies House snapshot and its indexes require several gigabytes.
`--limit` truncates the alphabetically ordered source data, so it is appropriate
only for development. `--active-only` excludes companies that are not currently
active.

Run the offline test suite with:

```bash
pytest
```

The candidate-generation integration tests require a disposable database:

```bash
TEST_DATABASE_URL=postgresql:///ch_test pytest
```

They intentionally ignore `DATABASE_URL` because they create and drop their own
schema.

## Architecture

```text
Streamlit UI
    |
    +-- Company resolver
    |     +-- local PostgreSQL name index
    |     +-- Python candidate ranking
    |     `-- live Companies House profile after user selection
    |
    `-- Research workflow
          +-- Companies House officers, filings, PSC, and charges
          +-- three parallel web-research specialists
          +-- citation validation against raw search responses
          +-- evidence auditor
          `-- deterministic Markdown report builder
```

### Company resolution

Registration numbers go directly to the Companies House API. Name searches use
a local copy of the Companies House Free Company Data Product because the
public search endpoint is not sufficiently typo tolerant.

The index stores current and former names in normalised and legal-suffix-free
forms. Candidate retrieval combines exact, one-edit, prefix, whole-string
trigram, and word-similarity matches. Python then ranks the shortlist using
token similarity, edit distance, n-gram overlap, company status, age, and
current-name preference.

Every name search returns candidates for the user to confirm, including a
single exact match. This prevents a well-supported report from being generated
for the wrong legal entity. An LLM-backed query suggester is used only when a
weak result needs correction or when the user explicitly asks for an
alternative interpretation.

The trigram thresholds and separate normalised/stem word-similarity branches
are intentional. Full-register testing showed that lower thresholds produced
very large candidate sets without improving recall, while an `OR` across the
two word-similarity columns could trigger a sequential scan.

### Research and grounding

Once the user confirms a company, the application fetches its officers, filing
history, persons with significant control, and registered charges. These
structured official records are converted to evidence directly and supplied as
context to all three web-research specialists.

The specialists cover business model, competitive landscape, and company
quality signals concurrently. Each produces cited research notes, followed by a
schema-constrained structuring pass. A web claim is admitted only when its URL
appears in the raw search response. Official Companies House claims bypass that
web-source check because the application constructs them from API responses.

The evidence auditor receives only registered source IDs. It merges duplicate
claims, handles contradictions, removes weak or unsupported claims, and
preserves evidence gaps. A final mechanical validation rejects any source ID
outside the allowed set before the report is rendered.

Agent tracing is disabled by default for Zero Data Retention compatibility.
Set `OPENAI_AGENTS_TRACING_ENABLED=true` where tracing is permitted.

Research specialists and company-name correction use `gpt-5.6-luna`; the
final evidence auditor uses `gpt-5.6-terra`. Override these roles with
`OPENAI_RESEARCH_MODEL`, `OPENAI_QUERY_MODEL`, and `OPENAI_AUDITOR_MODEL`.

## Scope and limitations

- The local name index is refreshed only when the loader is rerun. Live company
  details and official registers are fetched for every report.
- Companies House documents gaps in the bulk product's former-name data, so
  former-name matching is best effort.
- Reports are generated as downloadable Markdown; PDF export is not included.
- Research results are not cached, so repeated reports repeat external calls.
- Confidence values are qualitative evidence assessments, not calibrated
  probabilities.
- The application has no multi-user authentication or persistent report store.

Useful next steps would be a labelled recall@10 dataset for name ranking, an
evaluation set for evidence-audit decisions, time-bounded evidence caching, and
SIC code descriptions in the official record.
