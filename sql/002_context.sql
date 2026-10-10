ALTER TABLE v6.match_centre_invocations ADD COLUMN IF NOT EXISTS league_name text;
ALTER TABLE v6.match_centre_invocations ADD COLUMN IF NOT EXISTS home_team_logo text;
ALTER TABLE v6.match_centre_invocations ADD COLUMN IF NOT EXISTS away_team_logo text;
ALTER TABLE v6.match_centre_invocations ADD COLUMN IF NOT EXISTS source_raw jsonb;
