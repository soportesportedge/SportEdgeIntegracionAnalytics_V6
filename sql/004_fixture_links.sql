-- Independent daily catalogue and registry. No changes to the source database.
CREATE TABLE v6.fixture_link_catalogues (
    match_day date NOT NULL,
    provider text NOT NULL CHECK (provider IN ('365scores','betano')),
    events jsonb NOT NULL CHECK (jsonb_typeof(events)='array'),
    origin text NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now(),
    valid_until timestamptz NOT NULL,
    PRIMARY KEY (match_day,provider)
);
CREATE TABLE v6.fixture_link_runs (
    match_day date NOT NULL,
    provider text NOT NULL CHECK (provider IN ('365scores','betano')),
    status text NOT NULL CHECK (status IN ('running','completed','failed')),
    attempts integer NOT NULL DEFAULT 1,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    fixture_count integer NOT NULL DEFAULT 0,
    matched_count integer NOT NULL DEFAULT 0,
    last_error text,
    PRIMARY KEY (match_day,provider)
);
CREATE TABLE v6.fixture_link_registry (
    fixture_id bigint NOT NULL,
    match_day date NOT NULL,
    provider text NOT NULL CHECK (provider IN ('365scores','betano')),
    starting_at timestamptz NOT NULL,
    home_team_id bigint,
    away_team_id bigint,
    league_id bigint,
    status text NOT NULL CHECK (status IN ('matched','not_found','ambiguous','provider_error')),
    event_id text,
    url text,
    evidence jsonb NOT NULL DEFAULT '{}',
    resolved_at timestamptz NOT NULL DEFAULT now(),
    valid_until timestamptz NOT NULL,
    CHECK ((status='matched' AND event_id IS NOT NULL AND url IS NOT NULL) OR (status<>'matched' AND url IS NULL)),
    PRIMARY KEY (fixture_id,match_day,provider)
);
CREATE INDEX fixture_link_registry_day ON v6.fixture_link_registry(match_day,provider,status);
CREATE UNIQUE INDEX fixture_link_unique_event ON v6.fixture_link_registry(match_day,provider,event_id) WHERE status='matched';
CREATE TABLE v6.fixture_link_aliases (
    provider text NOT NULL CHECK (provider IN ('365scores','betano')),
    entity text NOT NULL CHECK (entity IN ('team','league')),
    source_id bigint NOT NULL,
    provider_name text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider,entity,source_id)
);
