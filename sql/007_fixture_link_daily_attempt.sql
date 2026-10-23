-- A failed daily batch must not turn later two-hour cycles into full-day scans.
ALTER TABLE v6.fixture_link_runs ADD COLUMN full_attempted_at timestamptz;
UPDATE v6.fixture_link_runs SET full_attempted_at=COALESCE(full_completed_at,
    CASE WHEN mode='full' THEN started_at END);
