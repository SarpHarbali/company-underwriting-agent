CREATE INDEX IF NOT EXISTS companies_company_number_idx
    ON companies (company_number);
CREATE INDEX IF NOT EXISTS companies_current_name_idx
    ON companies (company_number) WHERE NOT is_previous_name;

CREATE INDEX IF NOT EXISTS company_name_index_norm_pattern_idx
    ON company_name_index (name_norm text_pattern_ops);
CREATE INDEX IF NOT EXISTS company_name_index_stem_pattern_idx
    ON company_name_index (name_stem text_pattern_ops);

CREATE INDEX IF NOT EXISTS company_name_index_norm_trgm_idx
    ON company_name_index USING gin (name_norm gin_trgm_ops);
CREATE INDEX IF NOT EXISTS company_name_index_stem_trgm_idx
    ON company_name_index USING gin (name_stem gin_trgm_ops);
