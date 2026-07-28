-- Local mirror of the Companies House "Free Company Data Product" bulk file.
-- Applied by scripts/load_companies.py; see the README for provisioning notes.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- One row per *name*, not per company: a company with three former names has
-- four rows here, all sharing its company_number. The bulk CSV is wide
-- (PreviousName_1..10 as columns), so the loader unpivots it.
--
-- company_status, incorporation_date and postcode are properties of the
-- company and are therefore repeated identically on its previous-name rows.
-- That redundancy is deliberate: it lets candidate generation read everything
-- it needs to rank a match from the row it matched, without a join.
CREATE TABLE IF NOT EXISTS companies (
    -- Assigned by the loader rather than a sequence, so that a row and its
    -- company_name_index entry can be written in the same streaming pass.
    id                 bigint  PRIMARY KEY,
    company_number     text    NOT NULL,
    company_name       text    NOT NULL,   -- as filed; the only name text stored
    is_previous_name   boolean NOT NULL,
    company_status     text,
    incorporation_date date,
    postcode           text
);

-- Auxiliary retrieval table holding the normalised forms of every name in
-- `companies`. It exists so normalisation has exactly one implementation
-- (src/companies_house/names.py), shared by the loader and the query path. The
-- alternative - expression indexes over companies.company_name - would require
-- that logic restated as an IMMUTABLE SQL function, and any drift between the
-- two copies would silently stop names matching queries that should find them.
CREATE TABLE IF NOT EXISTS company_name_index (
    company_row_id bigint PRIMARY KEY REFERENCES companies(id) ON DELETE CASCADE,
    name_norm      text NOT NULL,  -- casefolded, punctuation- and accent-free
    name_stem      text NOT NULL   -- name_norm with legal suffixes stripped
);
