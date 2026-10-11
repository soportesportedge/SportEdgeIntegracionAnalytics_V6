-- One bounded read per batch, never one source request per alert.
SELECT f.id AS fixture_id, f.starting_at, f.home_team_id, f.away_team_id,
       f.league_id, COALESCE(f.home_team_name,ht.name) AS home,
       COALESCE(f.away_team_name,at.name) AS away,
       lg.name AS league, lg.country_name AS country
FROM fixtures f
LEFT JOIN teams ht ON ht.id=f.home_team_id
LEFT JOIN teams at ON at.id=f.away_team_id
LEFT JOIN leagues lg ON lg.id=f.league_id
WHERE f.starting_at >= $1::timestamptz AND f.starting_at < $2::timestamptz
ORDER BY f.starting_at,f.id
LIMIT $3;
