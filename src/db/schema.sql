CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS companies (
    id                 bigint  PRIMARY KEY,
    company_number     text    NOT NULL,
    company_name       text    NOT NULL,   
    is_previous_name   boolean NOT NULL,
    company_status     text,
    incorporation_date date,
    postcode           text
);

CREATE TABLE IF NOT EXISTS company_name_index (
    company_row_id bigint PRIMARY KEY REFERENCES companies(id) ON DELETE CASCADE,
    name_norm      text NOT NULL,  
    name_stem      text NOT NULL   
);
