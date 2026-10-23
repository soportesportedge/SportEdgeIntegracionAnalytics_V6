-- Per-provider scheduler progress; successful links remain in the existing cache.
ALTER TABLE v6.fixture_link_runs
    ADD COLUMN full_completed_at timestamptz,
    ADD COLUMN slot_at timestamptz,
    ADD COLUMN mode text NOT NULL DEFAULT 'full' CHECK (mode IN ('full','incremental')),
    ADD COLUMN window_start timestamptz,
    ADD COLUMN window_end timestamptz,
    ADD COLUMN candidate_count integer NOT NULL DEFAULT 0,
    ADD COLUMN preserved_count integer NOT NULL DEFAULT 0;

-- Earlier releases only ran full batches: preserve their successful daily work.
UPDATE v6.fixture_link_runs SET full_completed_at=finished_at,
    slot_at=(date_trunc('day',finished_at AT TIME ZONE 'America/Lima') AT TIME ZONE 'America/Lima')
        + make_interval(hours => (extract(hour FROM finished_at AT TIME ZONE 'America/Lima')::int / 2) * 2)
WHERE status='completed' AND finished_at IS NOT NULL;

-- The browser consumes these requests; no source DB credentials are required.
CREATE TABLE v6.fixture_link_catalogue_requests (
    match_day date NOT NULL,
    provider text NOT NULL CHECK (provider='betano'),
    request_id uuid NOT NULL UNIQUE,
    slot_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('pending','running','completed','failed')),
    attempts integer NOT NULL DEFAULT 0,
    requested_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    event_count integer NOT NULL DEFAULT 0,
    last_error text,
    PRIMARY KEY (match_day,provider)
);
CREATE INDEX fixture_link_catalogue_requests_due
    ON v6.fixture_link_catalogue_requests(status,next_attempt_at);
