CREATE SCHEMA IF NOT EXISTS stg;

CREATE TABLE IF NOT EXISTS stg.population (
    country_code      CHAR(3) NOT NULL CHECK (country_code ~ '^[A-Z]{3}$'),
    country_name      TEXT NOT NULL CHECK (length(trim(country_name)) > 0),
    year              SMALLINT NOT NULL CHECK (year BETWEEN 1900 AND 2100),
    population        BIGINT NOT NULL CHECK (population >= 0),
    indicator_code    TEXT NOT NULL CHECK (indicator_code = 'SP.POP.TOTL'),
    source_updated_at DATE NOT NULL,
    loaded_at         TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (country_code, year)
);
