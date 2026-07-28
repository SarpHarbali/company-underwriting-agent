-- Indexes for the local Companies House name index.
--
-- Kept separate from schema.sql because the loader creates them *after* the
-- bulk COPY: building a GIN trigram index on ~7M rows once at the end is
-- dramatically cheaper than maintaining it row by row during the load.

-- Resolution of a company number, and the lookup of a company's current name
-- from a row that matched one of its former names.
CREATE INDEX IF NOT EXISTS companies_company_number_idx
    ON companies (company_number);
CREATE INDEX IF NOT EXISTS companies_current_name_idx
    ON companies (company_number) WHERE NOT is_previous_name;

-- text_pattern_ops serves both the equality branches (exact and one-edit
-- variant matching) and the prefix branch ("monzo" -> "monzo bank limited").
-- A default-opclass btree would not support the prefix LIKE under a non-C
-- collation, and equality works fine on this opclass, so one index does both
-- jobs rather than two covering the same 7M rows.
CREATE INDEX IF NOT EXISTS company_name_index_norm_pattern_idx
    ON company_name_index (name_norm text_pattern_ops);
CREATE INDEX IF NOT EXISTS company_name_index_stem_pattern_idx
    ON company_name_index (name_stem text_pattern_ops);

-- The trigram branches: `%` (whole-string similarity) and `<%` (word
-- similarity, for a query matching a run of words inside a longer name).
CREATE INDEX IF NOT EXISTS company_name_index_norm_trgm_idx
    ON company_name_index USING gin (name_norm gin_trgm_ops);
CREATE INDEX IF NOT EXISTS company_name_index_stem_trgm_idx
    ON company_name_index USING gin (name_stem gin_trgm_ops);
