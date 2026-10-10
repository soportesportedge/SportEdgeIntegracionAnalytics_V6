WITH targets AS MATERIALIZED (
 SELECT m.* FROM match_centre_snapshots m WHERE m.id=ANY($1::bigint[])
), needed AS MATERIALIZED (
 SELECT id FROM targets
 UNION
 SELECT previous.id FROM targets t CROSS JOIN (VALUES (5),(10)) w(minutes)
 JOIN LATERAL (
   SELECT p.id FROM match_centre_snapshots p WHERE p.fixture_id=t.fixture_id
   AND p.snapshot_minute=t.snapshot_minute-w.minutes AND p.captured_at<t.captured_at
   AND p.home_team_id=t.home_team_id AND p.away_team_id=t.away_team_id
   AND p.state_developer_name='INPLAY_2ND_HALF' ORDER BY p.captured_at DESC,p.id DESC LIMIT 1
 ) previous ON true
), selected_h2h AS MATERIALIZED (
 SELECT DISTINCT previous.id FROM targets t JOIN LATERAL (
   SELECT h.id FROM head_to_head_snapshots h WHERE h.fixture_id=t.fixture_id
   AND h.home_team_id=t.home_team_id AND h.away_team_id=t.away_team_id AND h.captured_at<=t.captured_at
   ORDER BY h.captured_at DESC,h.id DESC LIMIT 1
 ) previous ON true
), params AS (
 SELECT NULL::timestamptz AS cutoff_at, NULL::timestamptz AS starting_from,
        NULL::timestamptz AS starting_until, 60::integer AS history_minute,
        120::integer AS max_odds_age_seconds
), cluster_mapping(league_id,cluster) AS (
 VALUES (1328,'EURO'),(2,'EURO'),(2286,'EURO'),(5,'EURO'),(582,'EURO'),(3156,'EURO'),(271,'EURO'),(301,'EURO'),(384,'EURO'),(564,'EURO'),(573,'EURO'),(8,'EURO'),(82,'EURO'),(567,'EURO'),(579,'EURO'),(743,'NORTEAMERICA'),(779,'NORTEAMERICA'),(1116,'SUDAMERICA'),(1098,'SUDAMERICA'),(636,'SUDAMERICA'),(648,'SUDAMERICA'),(672,'SUDAMERICA'),(764,'SUDAMERICA'),(651,'SUDAMERICA'),(767,'SUDAMERICA'),(732,'WORLDCUP')
), scoped AS MATERIALIZED (
 SELECT m.id,m.fixture_id,m.league_id,m.season_id,m.snapshot_minute,m.state_developer_name,
        m.home_team_id,m.away_team_id,m.home_score,m.away_score,m.captured_at,m.coverage_status,
        m.home_possession,m.away_possession,m.home_shots_total,m.away_shots_total,m.home_shots_on_target,m.away_shots_on_target,m.home_dangerous_attacks,m.away_dangerous_attacks,m.home_corners,m.away_corners,m.home_fouls,m.away_fouls,m.home_yellow_cards,m.away_yellow_cards,m.home_red_cards,m.away_red_cards,m.home_offsides,m.away_offsides,m.home_saves,m.away_saves,m.home_attacks,m.away_attacks,m.home_shots_off_target,m.away_shots_off_target,
        m.statistics,
        m.raw_payload->'statistics' AS raw_statistics,
        m.raw_payload->'events' AS raw_events,
        m.raw_payload->'periods' AS raw_periods,
        CASE WHEN m.raw_payload->>'stage_id' ~ '^[0-9]+$' THEN (m.raw_payload->>'stage_id')::bigint END AS stage_id,
        pi.second_half_period_id,pi.second_half_time_added
 FROM match_centre_snapshots m
 JOIN fixtures f ON f.id=m.fixture_id
 CROSS JOIN params cfg
 LEFT JOIN LATERAL (
   SELECT MAX((p->>'id')::bigint) FILTER(WHERE p->>'type_id'='2') AS second_half_period_id,
          MAX((p->>'time_added')::integer) FILTER(WHERE p->>'type_id'='2') AS second_half_time_added,
          COALESCE(BOOL_OR(p->>'type_id' IN ('3','4','5')),FALSE) AS contains_extra_period
   FROM jsonb_array_elements(CASE WHEN jsonb_typeof(m.raw_payload->'periods')='array' THEN m.raw_payload->'periods' ELSE '[]'::jsonb END) p
 ) pi ON TRUE
 WHERE m.id IN (SELECT id FROM needed) AND m.snapshot_minute>=cfg.history_minute
   AND m.state_developer_name IN ('INPLAY_2ND_HALF','FT')
   AND pi.contains_extra_period IS NOT TRUE
   AND pi.second_half_period_id IS NOT NULL
   AND (cfg.cutoff_at IS NULL OR m.captured_at<=cfg.cutoff_at)
   AND (cfg.starting_from IS NULL OR f.starting_at>=cfg.starting_from)
   AND (cfg.starting_until IS NULL OR f.starting_at<cfg.starting_until)
), normalized AS MATERIALIZED (
 SELECT s.id,s.fixture_id,s.league_id,s.season_id,s.snapshot_minute,s.state_developer_name,
        s.home_team_id,s.away_team_id,s.home_score,s.away_score,s.captured_at,s.coverage_status,
        s.raw_events,s.raw_periods,s.second_half_period_id,s.second_half_time_added,s.stage_id,
        COALESCE(rs.home_possession, CASE WHEN s.statistics->'home'->>'possession' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'home'->>'possession')::numeric END, s.home_possession)::numeric AS home_possession, COALESCE(rs.away_possession, CASE WHEN s.statistics->'away'->>'possession' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'away'->>'possession')::numeric END, s.away_possession)::numeric AS away_possession, COALESCE(rs.home_shots_total, CASE WHEN s.statistics->'home'->>'shots_total' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'home'->>'shots_total')::numeric END, s.home_shots_total)::integer AS home_shots_total, COALESCE(rs.away_shots_total, CASE WHEN s.statistics->'away'->>'shots_total' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'away'->>'shots_total')::numeric END, s.away_shots_total)::integer AS away_shots_total, COALESCE(rs.home_shots_on_target, CASE WHEN s.statistics->'home'->>'shots_on_target' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'home'->>'shots_on_target')::numeric END, s.home_shots_on_target)::integer AS home_shots_on_target, COALESCE(rs.away_shots_on_target, CASE WHEN s.statistics->'away'->>'shots_on_target' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'away'->>'shots_on_target')::numeric END, s.away_shots_on_target)::integer AS away_shots_on_target, COALESCE(rs.home_dangerous_attacks, CASE WHEN s.statistics->'home'->>'dangerous_attacks' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'home'->>'dangerous_attacks')::numeric END, s.home_dangerous_attacks)::integer AS home_dangerous_attacks, COALESCE(rs.away_dangerous_attacks, CASE WHEN s.statistics->'away'->>'dangerous_attacks' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'away'->>'dangerous_attacks')::numeric END, s.away_dangerous_attacks)::integer AS away_dangerous_attacks, COALESCE(rs.home_corners, CASE WHEN s.statistics->'home'->>'corners' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'home'->>'corners')::numeric END, s.home_corners)::integer AS home_corners, COALESCE(rs.away_corners, CASE WHEN s.statistics->'away'->>'corners' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'away'->>'corners')::numeric END, s.away_corners)::integer AS away_corners, COALESCE(rs.home_fouls, CASE WHEN s.statistics->'home'->>'fouls' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'home'->>'fouls')::numeric END, s.home_fouls)::integer AS home_fouls, COALESCE(rs.away_fouls, CASE WHEN s.statistics->'away'->>'fouls' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'away'->>'fouls')::numeric END, s.away_fouls)::integer AS away_fouls, COALESCE(rs.home_yellow_cards, CASE WHEN s.statistics->'home'->>'yellow_cards' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'home'->>'yellow_cards')::numeric END, s.home_yellow_cards)::integer AS home_yellow_cards, COALESCE(rs.away_yellow_cards, CASE WHEN s.statistics->'away'->>'yellow_cards' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'away'->>'yellow_cards')::numeric END, s.away_yellow_cards)::integer AS away_yellow_cards, COALESCE(rs.home_red_cards, CASE WHEN s.statistics->'home'->>'red_cards' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'home'->>'red_cards')::numeric END, s.home_red_cards)::integer AS home_red_cards, COALESCE(rs.away_red_cards, CASE WHEN s.statistics->'away'->>'red_cards' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'away'->>'red_cards')::numeric END, s.away_red_cards)::integer AS away_red_cards, COALESCE(rs.home_offsides, CASE WHEN s.statistics->'home'->>'offsides' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'home'->>'offsides')::numeric END, s.home_offsides)::integer AS home_offsides, COALESCE(rs.away_offsides, CASE WHEN s.statistics->'away'->>'offsides' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'away'->>'offsides')::numeric END, s.away_offsides)::integer AS away_offsides, COALESCE(rs.home_saves, CASE WHEN s.statistics->'home'->>'saves' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'home'->>'saves')::numeric END, s.home_saves)::integer AS home_saves, COALESCE(rs.away_saves, CASE WHEN s.statistics->'away'->>'saves' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'away'->>'saves')::numeric END, s.away_saves)::integer AS away_saves, COALESCE(rs.home_attacks, CASE WHEN s.statistics->'home'->>'attacks' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'home'->>'attacks')::numeric END, s.home_attacks)::integer AS home_attacks, COALESCE(rs.away_attacks, CASE WHEN s.statistics->'away'->>'attacks' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'away'->>'attacks')::numeric END, s.away_attacks)::integer AS away_attacks, COALESCE(rs.home_shots_off_target, CASE WHEN s.statistics->'home'->>'shots_off_target' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'home'->>'shots_off_target')::numeric END, s.home_shots_off_target)::integer AS home_shots_off_target, COALESCE(rs.away_shots_off_target, CASE WHEN s.statistics->'away'->>'shots_off_target' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'away'->>'shots_off_target')::numeric END, s.away_shots_off_target)::integer AS away_shots_off_target, COALESCE(rs.home_shots_insidebox, CASE WHEN s.statistics->'home'->>'shots_insidebox' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'home'->>'shots_insidebox')::numeric END)::integer AS home_shots_insidebox, COALESCE(rs.away_shots_insidebox, CASE WHEN s.statistics->'away'->>'shots_insidebox' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'away'->>'shots_insidebox')::numeric END)::integer AS away_shots_insidebox, COALESCE(rs.home_shots_outsidebox, CASE WHEN s.statistics->'home'->>'shots_outsidebox' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'home'->>'shots_outsidebox')::numeric END)::integer AS home_shots_outsidebox, COALESCE(rs.away_shots_outsidebox, CASE WHEN s.statistics->'away'->>'shots_outsidebox' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'away'->>'shots_outsidebox')::numeric END)::integer AS away_shots_outsidebox, COALESCE(rs.home_shots_blocked, CASE WHEN s.statistics->'home'->>'shots_blocked' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'home'->>'shots_blocked')::numeric END)::integer AS home_shots_blocked, COALESCE(rs.away_shots_blocked, CASE WHEN s.statistics->'away'->>'shots_blocked' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'away'->>'shots_blocked')::numeric END)::integer AS away_shots_blocked, COALESCE(rs.home_key_passes, CASE WHEN s.statistics->'home'->>'key_passes' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'home'->>'key_passes')::numeric END)::integer AS home_key_passes, COALESCE(rs.away_key_passes, CASE WHEN s.statistics->'away'->>'key_passes' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'away'->>'key_passes')::numeric END)::integer AS away_key_passes, COALESCE(rs.home_big_chances_created, CASE WHEN s.statistics->'home'->>'big_chances_created' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'home'->>'big_chances_created')::numeric END)::integer AS home_big_chances_created, COALESCE(rs.away_big_chances_created, CASE WHEN s.statistics->'away'->>'big_chances_created' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'away'->>'big_chances_created')::numeric END)::integer AS away_big_chances_created, COALESCE(rs.home_big_chances_missed, CASE WHEN s.statistics->'home'->>'big_chances_missed' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'home'->>'big_chances_missed')::numeric END)::integer AS home_big_chances_missed, COALESCE(rs.away_big_chances_missed, CASE WHEN s.statistics->'away'->>'big_chances_missed' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'away'->>'big_chances_missed')::numeric END)::integer AS away_big_chances_missed, COALESCE(rs.home_substitutions, CASE WHEN s.statistics->'home'->>'substitutions' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'home'->>'substitutions')::numeric END)::integer AS home_substitutions, COALESCE(rs.away_substitutions, CASE WHEN s.statistics->'away'->>'substitutions' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'away'->>'substitutions')::numeric END)::integer AS away_substitutions, COALESCE(rs.home_passes, CASE WHEN s.statistics->'home'->>'passes' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'home'->>'passes')::numeric END)::integer AS home_passes, COALESCE(rs.away_passes, CASE WHEN s.statistics->'away'->>'passes' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'away'->>'passes')::numeric END)::integer AS away_passes, COALESCE(rs.home_passes_accuracy_pct, CASE WHEN s.statistics->'home'->>'passes_accuracy_pct' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'home'->>'passes_accuracy_pct')::numeric END)::numeric AS home_passes_accuracy_pct, COALESCE(rs.away_passes_accuracy_pct, CASE WHEN s.statistics->'away'->>'passes_accuracy_pct' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'away'->>'passes_accuracy_pct')::numeric END)::numeric AS away_passes_accuracy_pct, COALESCE(rs.home_crosses_total, CASE WHEN s.statistics->'home'->>'crosses_total' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'home'->>'crosses_total')::numeric END)::integer AS home_crosses_total, COALESCE(rs.away_crosses_total, CASE WHEN s.statistics->'away'->>'crosses_total' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'away'->>'crosses_total')::numeric END)::integer AS away_crosses_total, COALESCE(rs.home_crosses_accurate, CASE WHEN s.statistics->'home'->>'crosses_accurate' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'home'->>'crosses_accurate')::numeric END)::integer AS home_crosses_accurate, COALESCE(rs.away_crosses_accurate, CASE WHEN s.statistics->'away'->>'crosses_accurate' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'away'->>'crosses_accurate')::numeric END)::integer AS away_crosses_accurate, COALESCE(rs.home_interceptions, CASE WHEN s.statistics->'home'->>'interceptions' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'home'->>'interceptions')::numeric END)::integer AS home_interceptions, COALESCE(rs.away_interceptions, CASE WHEN s.statistics->'away'->>'interceptions' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'away'->>'interceptions')::numeric END)::integer AS away_interceptions, COALESCE(rs.home_tackles, CASE WHEN s.statistics->'home'->>'tackles' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'home'->>'tackles')::numeric END)::integer AS home_tackles, COALESCE(rs.away_tackles, CASE WHEN s.statistics->'away'->>'tackles' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'away'->>'tackles')::numeric END)::integer AS away_tackles, COALESCE(rs.home_duels_won, CASE WHEN s.statistics->'home'->>'duels_won' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'home'->>'duels_won')::numeric END)::integer AS home_duels_won, COALESCE(rs.away_duels_won, CASE WHEN s.statistics->'away'->>'duels_won' ~ '^-?[0-9]+([.][0-9]+)?$' THEN (s.statistics->'away'->>'duels_won')::numeric END)::integer AS away_duels_won
 FROM scoped s
 LEFT JOIN LATERAL (
   SELECT MAX(v.value) FILTER (WHERE v.team_id=s.home_team_id AND v.type_name IN ('Ball Possession','Ball Possession %')) AS home_possession, MAX(v.value) FILTER (WHERE v.team_id=s.away_team_id AND v.type_name IN ('Ball Possession','Ball Possession %')) AS away_possession, MAX(v.value) FILTER (WHERE v.team_id=s.home_team_id AND v.type_name IN ('Shots Total')) AS home_shots_total, MAX(v.value) FILTER (WHERE v.team_id=s.away_team_id AND v.type_name IN ('Shots Total')) AS away_shots_total, MAX(v.value) FILTER (WHERE v.team_id=s.home_team_id AND v.type_name IN ('Shots On Target')) AS home_shots_on_target, MAX(v.value) FILTER (WHERE v.team_id=s.away_team_id AND v.type_name IN ('Shots On Target')) AS away_shots_on_target, MAX(v.value) FILTER (WHERE v.team_id=s.home_team_id AND v.type_name IN ('Dangerous Attacks')) AS home_dangerous_attacks, MAX(v.value) FILTER (WHERE v.team_id=s.away_team_id AND v.type_name IN ('Dangerous Attacks')) AS away_dangerous_attacks, MAX(v.value) FILTER (WHERE v.team_id=s.home_team_id AND v.type_name IN ('Corners')) AS home_corners, MAX(v.value) FILTER (WHERE v.team_id=s.away_team_id AND v.type_name IN ('Corners')) AS away_corners, MAX(v.value) FILTER (WHERE v.team_id=s.home_team_id AND v.type_name IN ('Fouls')) AS home_fouls, MAX(v.value) FILTER (WHERE v.team_id=s.away_team_id AND v.type_name IN ('Fouls')) AS away_fouls, MAX(v.value) FILTER (WHERE v.team_id=s.home_team_id AND v.type_name IN ('Yellowcards','Yellow Cards')) AS home_yellow_cards, MAX(v.value) FILTER (WHERE v.team_id=s.away_team_id AND v.type_name IN ('Yellowcards','Yellow Cards')) AS away_yellow_cards, MAX(v.value) FILTER (WHERE v.team_id=s.home_team_id AND v.type_name IN ('Redcards','Red Cards')) AS home_red_cards, MAX(v.value) FILTER (WHERE v.team_id=s.away_team_id AND v.type_name IN ('Redcards','Red Cards')) AS away_red_cards, MAX(v.value) FILTER (WHERE v.team_id=s.home_team_id AND v.type_name IN ('Offsides')) AS home_offsides, MAX(v.value) FILTER (WHERE v.team_id=s.away_team_id AND v.type_name IN ('Offsides')) AS away_offsides, MAX(v.value) FILTER (WHERE v.team_id=s.home_team_id AND v.type_name IN ('Saves')) AS home_saves, MAX(v.value) FILTER (WHERE v.team_id=s.away_team_id AND v.type_name IN ('Saves')) AS away_saves, MAX(v.value) FILTER (WHERE v.team_id=s.home_team_id AND v.type_name IN ('Attacks')) AS home_attacks, MAX(v.value) FILTER (WHERE v.team_id=s.away_team_id AND v.type_name IN ('Attacks')) AS away_attacks, MAX(v.value) FILTER (WHERE v.team_id=s.home_team_id AND v.type_name IN ('Shots Off Target')) AS home_shots_off_target, MAX(v.value) FILTER (WHERE v.team_id=s.away_team_id AND v.type_name IN ('Shots Off Target')) AS away_shots_off_target, MAX(v.value) FILTER (WHERE v.team_id=s.home_team_id AND v.type_name IN ('Shots Insidebox')) AS home_shots_insidebox, MAX(v.value) FILTER (WHERE v.team_id=s.away_team_id AND v.type_name IN ('Shots Insidebox')) AS away_shots_insidebox, MAX(v.value) FILTER (WHERE v.team_id=s.home_team_id AND v.type_name IN ('Shots Outsidebox')) AS home_shots_outsidebox, MAX(v.value) FILTER (WHERE v.team_id=s.away_team_id AND v.type_name IN ('Shots Outsidebox')) AS away_shots_outsidebox, MAX(v.value) FILTER (WHERE v.team_id=s.home_team_id AND v.type_name IN ('Shots Blocked')) AS home_shots_blocked, MAX(v.value) FILTER (WHERE v.team_id=s.away_team_id AND v.type_name IN ('Shots Blocked')) AS away_shots_blocked, MAX(v.value) FILTER (WHERE v.team_id=s.home_team_id AND v.type_name IN ('Key Passes')) AS home_key_passes, MAX(v.value) FILTER (WHERE v.team_id=s.away_team_id AND v.type_name IN ('Key Passes')) AS away_key_passes, MAX(v.value) FILTER (WHERE v.team_id=s.home_team_id AND v.type_name IN ('Big Chances Created')) AS home_big_chances_created, MAX(v.value) FILTER (WHERE v.team_id=s.away_team_id AND v.type_name IN ('Big Chances Created')) AS away_big_chances_created, MAX(v.value) FILTER (WHERE v.team_id=s.home_team_id AND v.type_name IN ('Big Chances Missed')) AS home_big_chances_missed, MAX(v.value) FILTER (WHERE v.team_id=s.away_team_id AND v.type_name IN ('Big Chances Missed')) AS away_big_chances_missed, MAX(v.value) FILTER (WHERE v.team_id=s.home_team_id AND v.type_name IN ('Substitutions')) AS home_substitutions, MAX(v.value) FILTER (WHERE v.team_id=s.away_team_id AND v.type_name IN ('Substitutions')) AS away_substitutions, MAX(v.value) FILTER (WHERE v.team_id=s.home_team_id AND v.type_name IN ('Passes')) AS home_passes, MAX(v.value) FILTER (WHERE v.team_id=s.away_team_id AND v.type_name IN ('Passes')) AS away_passes, MAX(v.value) FILTER (WHERE v.team_id=s.home_team_id AND v.type_name IN ('Successful Passes Percentage')) AS home_passes_accuracy_pct, MAX(v.value) FILTER (WHERE v.team_id=s.away_team_id AND v.type_name IN ('Successful Passes Percentage')) AS away_passes_accuracy_pct, MAX(v.value) FILTER (WHERE v.team_id=s.home_team_id AND v.type_name IN ('Total Crosses')) AS home_crosses_total, MAX(v.value) FILTER (WHERE v.team_id=s.away_team_id AND v.type_name IN ('Total Crosses')) AS away_crosses_total, MAX(v.value) FILTER (WHERE v.team_id=s.home_team_id AND v.type_name IN ('Accurate Crosses')) AS home_crosses_accurate, MAX(v.value) FILTER (WHERE v.team_id=s.away_team_id AND v.type_name IN ('Accurate Crosses')) AS away_crosses_accurate, MAX(v.value) FILTER (WHERE v.team_id=s.home_team_id AND v.type_name IN ('Interceptions')) AS home_interceptions, MAX(v.value) FILTER (WHERE v.team_id=s.away_team_id AND v.type_name IN ('Interceptions')) AS away_interceptions, MAX(v.value) FILTER (WHERE v.team_id=s.home_team_id AND v.type_name IN ('Tackles')) AS home_tackles, MAX(v.value) FILTER (WHERE v.team_id=s.away_team_id AND v.type_name IN ('Tackles')) AS away_tackles, MAX(v.value) FILTER (WHERE v.team_id=s.home_team_id AND v.type_name IN ('Duels Won')) AS home_duels_won, MAX(v.value) FILTER (WHERE v.team_id=s.away_team_id AND v.type_name IN ('Duels Won')) AS away_duels_won
   FROM (
      SELECT CASE WHEN COALESCE(r->>'participant_id',r->>'team_id') ~ '^[0-9]+$' THEN COALESCE(r->>'participant_id',r->>'team_id')::bigint END AS team_id,
             COALESCE(r->'type'->>'name',r->>'type_name') AS type_name,
             CASE WHEN COALESCE(r->'data'->>'value',r->>'value') ~ '^-?[0-9]+([.][0-9]+)?$' THEN COALESCE(r->'data'->>'value',r->>'value')::numeric END AS value
      FROM jsonb_array_elements(CASE WHEN jsonb_typeof(s.raw_statistics)='array' THEN s.raw_statistics ELSE '[]'::jsonb END) r
   ) v
 ) rs ON TRUE
), h2h_corrected AS MATERIALIZED (
 SELECT h.id,h.fixture_id,h.home_team_id,h.away_team_id,h.captured_at,
        COUNT(g.home_goals)::bigint AS total_matches,
        COUNT(*) FILTER(WHERE g.home_goals>g.away_goals)::bigint AS home_wins,
        COUNT(*) FILTER(WHERE g.home_goals=g.away_goals)::bigint AS draws,
        COUNT(*) FILTER(WHERE g.home_goals<g.away_goals)::bigint AS away_wins,
        COALESCE(SUM(g.home_goals),0)::bigint AS home_goals,
        COALESCE(SUM(g.away_goals),0)::bigint AS away_goals
 FROM head_to_head_snapshots h
 JOIN selected_h2h chosen ON chosen.id=h.id
 CROSS JOIN params cfg
 LEFT JOIN LATERAL (
   SELECT DISTINCT ON (a->>'id') a
   FROM jsonb_array_elements(CASE WHEN jsonb_typeof(h.raw_payload->'data')='array' THEN h.raw_payload->'data' ELSE '[]'::jsonb END) a
   WHERE a->>'id'<>h.fixture_id::text
     AND COALESCE(a->'state'->>'developer_name',a->'state'->>'state')='FT'
     AND (a->>'starting_at')::timestamp AT TIME ZONE 'UTC'<h.captured_at
 ) old ON TRUE
 LEFT JOIN LATERAL (
   SELECT MAX((sc->'score'->>'goals')::integer) FILTER(WHERE sc->>'participant_id'=h.home_team_id::text) AS home_goals,
          MAX((sc->'score'->>'goals')::integer) FILTER(WHERE sc->>'participant_id'=h.away_team_id::text) AS away_goals
   FROM jsonb_array_elements(CASE WHEN jsonb_typeof(old.a->'scores')='array' THEN old.a->'scores' ELSE '[]'::jsonb END) sc
   WHERE sc->>'description'='CURRENT'
 ) g ON g.home_goals IS NOT NULL AND g.away_goals IS NOT NULL
 WHERE (cfg.cutoff_at IS NULL OR h.captured_at<=cfg.cutoff_at)
 GROUP BY h.id,h.fixture_id,h.home_team_id,h.away_team_id,h.captured_at
)
SELECT
    lg.name AS league_name, ht.image_path AS home_team_logo, at.image_path AS away_team_logo,
    src.raw_payload AS source_raw, fx.starting_at AS starting_at,
    mc.captured_at AS captured_at,
    mc.fixture_id AS id,
    mc.league_id AS league_id,
    mc.home_team_id AS home_team_id,
    fx.home_team_name AS home_team_name,
    mc.away_team_id AS away_team_id,
    fx.away_team_name AS away_team_name,
    COALESCE(cm.cluster,'SIN_ASIGNAR') AS cluster,
    mc.snapshot_minute AS snapshot_minute,
    mc.home_score AS home_score,
    mc.away_score AS away_score,
    mc.home_score-mc.away_score AS dif_score,
    mc.home_possession AS home_possession,
    mc.away_possession AS away_possession,
    mc.home_possession-mc.away_possession AS dif_possession,
    mc.home_shots_total AS home_shots_total,
    mc.away_shots_total AS away_shots_total,
    mc.home_shots_total-mc.away_shots_total AS dif_shots_total,
    mc.home_shots_on_target AS home_shots_on_target,
    mc.away_shots_on_target AS away_shots_on_target,
    mc.home_shots_on_target-mc.away_shots_on_target AS dif_shots_on_target,
    mc.home_dangerous_attacks AS home_dangerous_attacks,
    mc.away_dangerous_attacks AS away_dangerous_attacks,
    mc.home_dangerous_attacks-mc.away_dangerous_attacks AS dif_dangerous_attacks,
    mc.home_corners AS home_corners,
    mc.away_corners AS away_corners,
    mc.home_corners-mc.away_corners AS dif_corners,
    mc.home_fouls AS home_fouls,
    mc.away_fouls AS away_fouls,
    mc.home_fouls-mc.away_fouls AS dif_fauls,
    mc.home_yellow_cards AS home_yellow_cards,
    mc.away_yellow_cards AS away_yellow_cards,
    mc.home_yellow_cards-mc.away_yellow_cards AS dif_yellow_cards,
    mc.home_red_cards AS home_red_cards,
    mc.away_red_cards AS away_red_cards,
    mc.home_red_cards-mc.away_red_cards AS dif_red_cards,
    mc.home_offsides AS home_offsides,
    mc.away_offsides AS away_offsides,
    mc.home_offsides-mc.away_offsides AS dif_offsides,
    mc.home_saves AS home_saves,
    mc.away_saves AS away_saves,
    mc.home_saves-mc.away_saves AS dif_saves,
    mc.home_attacks AS home_attacks,
    mc.away_attacks AS away_attacks,
    mc.home_attacks-mc.away_attacks AS dif_attacks,
    mc.home_shots_off_target AS home_shots_off_target,
    mc.away_shots_off_target AS away_shots_off_target,
    mc.home_shots_off_target-mc.away_shots_off_target AS dif_shots_off_target,
    hh.total_matches AS h2h_total_matches,
    hh.home_wins AS h2h_home_wins,
    hh.draws AS h2h_draw,
    hh.away_wins AS h2h_away_wins,
    hh.home_wins-hh.away_wins AS dif_h2h_wins,
    hh.home_goals AS h2h_home_goals,
    hh.away_goals AS h2h_away_goals,
    hh.home_goals-hh.away_goals AS dif_h2h_goal,
    sh.position AS position_home,
    sh.points AS points_home,
    sh.goal_diff AS goal_diff_home,
    sh.goals_against AS goals_against_home,
    sh.goals_for AS goals_for_home,
    sh.wins AS wins_home,
    sh.draws AS draws_home,
    sh.losses AS losses_home,
    sa.position AS position_away,
    sa.points AS points_away,
    sa.goal_diff AS goal_diff_away,
    sa.goals_against AS goals_against_away,
    sa.goals_for AS goals_for_away,
    sa.wins AS wins_away,
    sa.draws AS draws_away,
    sa.losses AS losses_away,
    sh.position-sa.position AS dif_position,
    CASE WHEN odds.valid THEN 1 ELSE NULL END AS market_id,
    CASE WHEN odds.valid THEN o.odds_home END AS odds_home,
    CASE WHEN odds.valid THEN o.odds_draw END AS odds_draw,
    CASE WHEN odds.valid THEN o.odds_away END AS odds_away,
    CASE WHEN odds.valid THEN (1.0/o.odds_home) / (1.0/o.odds_home+1.0/o.odds_draw+1.0/o.odds_away) END AS implied_prob_home,
    CASE WHEN odds.valid THEN (1.0/o.odds_draw) / (1.0/o.odds_home+1.0/o.odds_draw+1.0/o.odds_away) END AS implied_prob_draw,
    CASE WHEN odds.valid THEN (1.0/o.odds_away) / (1.0/o.odds_home+1.0/o.odds_draw+1.0/o.odds_away) END AS implied_prob_away,
    prev.home_score_increased AS home_score_increased_prev_10m,
    prev.away_score_increased AS away_score_increased_prev_10m,
    mc.id AS match_centre_snapshot_id,
    mc.season_id AS season_id,
    mc.state_developer_name AS state_developer_name,
    mc.state_developer_name='FT' AS is_postmatch_snapshot,
    mc.state_developer_name='INPLAY_2ND_HALF' AS is_live_training_snapshot,
    mc.coverage_status AS coverage_status,
    hh.id AS h2h_snapshot_id,
    EXTRACT(EPOCH FROM mc.captured_at-hh.captured_at) AS h2h_age_seconds,
    COALESCE(hh.total_matches>0,FALSE) AS has_h2h,
    mc.home_shots_insidebox AS home_shots_insidebox,
    mc.away_shots_insidebox AS away_shots_insidebox,
    mc.home_shots_insidebox-mc.away_shots_insidebox AS dif_shots_insidebox,
    mc.home_shots_outsidebox AS home_shots_outsidebox,
    mc.away_shots_outsidebox AS away_shots_outsidebox,
    mc.home_shots_outsidebox-mc.away_shots_outsidebox AS dif_shots_outsidebox,
    mc.home_shots_blocked AS home_shots_blocked,
    mc.away_shots_blocked AS away_shots_blocked,
    mc.home_shots_blocked-mc.away_shots_blocked AS dif_shots_blocked,
    mc.home_key_passes AS home_key_passes,
    mc.away_key_passes AS away_key_passes,
    mc.home_key_passes-mc.away_key_passes AS dif_key_passes,
    mc.home_big_chances_created AS home_big_chances_created,
    mc.away_big_chances_created AS away_big_chances_created,
    mc.home_big_chances_created-mc.away_big_chances_created AS dif_big_chances_created,
    mc.home_big_chances_missed AS home_big_chances_missed,
    mc.away_big_chances_missed AS away_big_chances_missed,
    mc.home_big_chances_missed-mc.away_big_chances_missed AS dif_big_chances_missed,
    mc.home_substitutions AS home_substitutions,
    mc.away_substitutions AS away_substitutions,
    mc.home_substitutions-mc.away_substitutions AS dif_substitutions,
    mc.home_passes AS home_passes,
    mc.away_passes AS away_passes,
    mc.home_passes-mc.away_passes AS dif_passes,
    mc.home_passes_accuracy_pct AS home_passes_accuracy_pct,
    mc.away_passes_accuracy_pct AS away_passes_accuracy_pct,
    mc.home_passes_accuracy_pct-mc.away_passes_accuracy_pct AS dif_passes_accuracy_pct,
    mc.home_crosses_total AS home_crosses_total,
    mc.away_crosses_total AS away_crosses_total,
    mc.home_crosses_total-mc.away_crosses_total AS dif_crosses_total,
    mc.home_crosses_accurate AS home_crosses_accurate,
    mc.away_crosses_accurate AS away_crosses_accurate,
    mc.home_crosses_accurate-mc.away_crosses_accurate AS dif_crosses_accurate,
    mc.home_interceptions AS home_interceptions,
    mc.away_interceptions AS away_interceptions,
    mc.home_interceptions-mc.away_interceptions AS dif_interceptions,
    mc.home_tackles AS home_tackles,
    mc.away_tackles AS away_tackles,
    mc.home_tackles-mc.away_tackles AS dif_tackles,
    mc.home_duels_won AS home_duels_won,
    mc.away_duels_won AS away_duels_won,
    mc.home_duels_won-mc.away_duels_won AS dif_duels_won,
    mc.home_shots_insidebox::numeric / NULLIF(mc.home_shots_total,0) AS home_shots_insidebox_share,
    mc.home_shots_on_target::numeric / NULLIF(mc.home_shots_total,0) AS home_shots_on_target_share,
    mc.home_crosses_accurate::numeric / NULLIF(mc.home_crosses_total,0) AS home_crosses_accuracy_share,
    mc.away_shots_insidebox::numeric / NULLIF(mc.away_shots_total,0) AS away_shots_insidebox_share,
    mc.away_shots_on_target::numeric / NULLIF(mc.away_shots_total,0) AS away_shots_on_target_share,
    mc.away_crosses_accurate::numeric / NULLIF(mc.away_crosses_total,0) AS away_crosses_accuracy_share,
    sh.games_played AS games_played_home,
    sh.points::numeric / NULLIF(sh.games_played,0) AS points_per_game_home,
    sh.goals_for::numeric / NULLIF(sh.games_played,0) AS goals_for_per_game_home,
    sh.goals_against::numeric / NULLIF(sh.games_played,0) AS goals_against_per_game_home,
    sh.wins::numeric / NULLIF(sh.games_played,0) AS win_rate_home,
    EXTRACT(EPOCH FROM mc.captured_at-sh.captured_at) AS standings_age_seconds_home,
    sh.id IS NOT NULL AS has_standings_home,
    sa.games_played AS games_played_away,
    sa.points::numeric / NULLIF(sa.games_played,0) AS points_per_game_away,
    sa.goals_for::numeric / NULLIF(sa.games_played,0) AS goals_for_per_game_away,
    sa.goals_against::numeric / NULLIF(sa.games_played,0) AS goals_against_per_game_away,
    sa.wins::numeric / NULLIF(sa.games_played,0) AS win_rate_away,
    EXTRACT(EPOCH FROM mc.captured_at-sa.captured_at) AS standings_age_seconds_away,
    sa.id IS NOT NULL AS has_standings_away,
    (hh.home_wins)::numeric / NULLIF(hh.total_matches,0) AS h2h_home_win_rate,
    (hh.away_wins)::numeric / NULLIF(hh.total_matches,0) AS h2h_away_win_rate,
    (hh.draws)::numeric / NULLIF(hh.total_matches,0) AS h2h_draw_rate,
    (hh.home_goals+hh.away_goals)::numeric / NULLIF(hh.total_matches,0) AS h2h_goals_per_match,
    mc.home_score+mc.away_score AS total_goals_now,
    ABS(mc.home_score-mc.away_score) AS abs_score_diff,
    COALESCE(mc.home_score<mc.away_score,FALSE) AS is_home_losing,
    COALESCE(mc.away_score<mc.home_score,FALSE) AS is_away_losing,
    COALESCE(mc.home_score=mc.away_score,FALSE) AS is_draw_now,
    mc.home_score IS NOT NULL AND mc.away_score IS NOT NULL AS has_current_score,
    prev.has_score_history AS has_score_history_10m,
    b5.id IS NOT NULL AS has_history_5m,
    EXTRACT(EPOCH FROM mc.captured_at-b5.captured_at) AS history_capture_age_seconds_5m,
    COALESCE((mc.home_shots_total<b5.home_shots_total OR mc.home_shots_on_target<b5.home_shots_on_target OR mc.home_shots_insidebox<b5.home_shots_insidebox OR mc.home_dangerous_attacks<b5.home_dangerous_attacks OR mc.home_corners<b5.home_corners OR mc.away_shots_total<b5.away_shots_total OR mc.away_shots_on_target<b5.away_shots_on_target OR mc.away_shots_insidebox<b5.away_shots_insidebox OR mc.away_dangerous_attacks<b5.away_dangerous_attacks OR mc.away_corners<b5.away_corners OR mc.home_score<b5.home_score OR mc.away_score<b5.away_score),FALSE) AS counter_correction_5m,
    CASE WHEN mc.home_shots_total>=b5.home_shots_total THEN mc.home_shots_total-b5.home_shots_total END AS home_shots_total_last_5m,
    CASE WHEN mc.away_shots_total>=b5.away_shots_total THEN mc.away_shots_total-b5.away_shots_total END AS away_shots_total_last_5m,
    CASE WHEN mc.home_shots_total>=b5.home_shots_total AND mc.away_shots_total>=b5.away_shots_total THEN (mc.home_shots_total-b5.home_shots_total)-(mc.away_shots_total-b5.away_shots_total) END AS dif_shots_total_last_5m,
    CASE WHEN mc.home_shots_on_target>=b5.home_shots_on_target THEN mc.home_shots_on_target-b5.home_shots_on_target END AS home_shots_on_target_last_5m,
    CASE WHEN mc.away_shots_on_target>=b5.away_shots_on_target THEN mc.away_shots_on_target-b5.away_shots_on_target END AS away_shots_on_target_last_5m,
    CASE WHEN mc.home_shots_on_target>=b5.home_shots_on_target AND mc.away_shots_on_target>=b5.away_shots_on_target THEN (mc.home_shots_on_target-b5.home_shots_on_target)-(mc.away_shots_on_target-b5.away_shots_on_target) END AS dif_shots_on_target_last_5m,
    CASE WHEN mc.home_shots_insidebox>=b5.home_shots_insidebox THEN mc.home_shots_insidebox-b5.home_shots_insidebox END AS home_shots_insidebox_last_5m,
    CASE WHEN mc.away_shots_insidebox>=b5.away_shots_insidebox THEN mc.away_shots_insidebox-b5.away_shots_insidebox END AS away_shots_insidebox_last_5m,
    CASE WHEN mc.home_shots_insidebox>=b5.home_shots_insidebox AND mc.away_shots_insidebox>=b5.away_shots_insidebox THEN (mc.home_shots_insidebox-b5.home_shots_insidebox)-(mc.away_shots_insidebox-b5.away_shots_insidebox) END AS dif_shots_insidebox_last_5m,
    CASE WHEN mc.home_dangerous_attacks>=b5.home_dangerous_attacks THEN mc.home_dangerous_attacks-b5.home_dangerous_attacks END AS home_dangerous_attacks_last_5m,
    CASE WHEN mc.away_dangerous_attacks>=b5.away_dangerous_attacks THEN mc.away_dangerous_attacks-b5.away_dangerous_attacks END AS away_dangerous_attacks_last_5m,
    CASE WHEN mc.home_dangerous_attacks>=b5.home_dangerous_attacks AND mc.away_dangerous_attacks>=b5.away_dangerous_attacks THEN (mc.home_dangerous_attacks-b5.home_dangerous_attacks)-(mc.away_dangerous_attacks-b5.away_dangerous_attacks) END AS dif_dangerous_attacks_last_5m,
    CASE WHEN mc.home_corners>=b5.home_corners THEN mc.home_corners-b5.home_corners END AS home_corners_last_5m,
    CASE WHEN mc.away_corners>=b5.away_corners THEN mc.away_corners-b5.away_corners END AS away_corners_last_5m,
    CASE WHEN mc.home_corners>=b5.home_corners AND mc.away_corners>=b5.away_corners THEN (mc.home_corners-b5.home_corners)-(mc.away_corners-b5.away_corners) END AS dif_corners_last_5m,
    CASE WHEN mc.home_score<mc.away_score AND mc.home_shots_on_target>=b5.home_shots_on_target THEN mc.home_shots_on_target-b5.home_shots_on_target WHEN mc.away_score<mc.home_score AND mc.away_shots_on_target>=b5.away_shots_on_target THEN mc.away_shots_on_target-b5.away_shots_on_target END AS trailing_team_shots_on_target_last_5m,
    b10.id IS NOT NULL AS has_history_10m,
    EXTRACT(EPOCH FROM mc.captured_at-b10.captured_at) AS history_capture_age_seconds_10m,
    COALESCE((mc.home_shots_total<b10.home_shots_total OR mc.home_shots_on_target<b10.home_shots_on_target OR mc.home_shots_insidebox<b10.home_shots_insidebox OR mc.home_dangerous_attacks<b10.home_dangerous_attacks OR mc.home_corners<b10.home_corners OR mc.away_shots_total<b10.away_shots_total OR mc.away_shots_on_target<b10.away_shots_on_target OR mc.away_shots_insidebox<b10.away_shots_insidebox OR mc.away_dangerous_attacks<b10.away_dangerous_attacks OR mc.away_corners<b10.away_corners OR mc.home_score<b10.home_score OR mc.away_score<b10.away_score),FALSE) AS counter_correction_10m,
    CASE WHEN mc.home_shots_total>=b10.home_shots_total THEN mc.home_shots_total-b10.home_shots_total END AS home_shots_total_last_10m,
    CASE WHEN mc.away_shots_total>=b10.away_shots_total THEN mc.away_shots_total-b10.away_shots_total END AS away_shots_total_last_10m,
    CASE WHEN mc.home_shots_total>=b10.home_shots_total AND mc.away_shots_total>=b10.away_shots_total THEN (mc.home_shots_total-b10.home_shots_total)-(mc.away_shots_total-b10.away_shots_total) END AS dif_shots_total_last_10m,
    CASE WHEN mc.home_shots_on_target>=b10.home_shots_on_target THEN mc.home_shots_on_target-b10.home_shots_on_target END AS home_shots_on_target_last_10m,
    CASE WHEN mc.away_shots_on_target>=b10.away_shots_on_target THEN mc.away_shots_on_target-b10.away_shots_on_target END AS away_shots_on_target_last_10m,
    CASE WHEN mc.home_shots_on_target>=b10.home_shots_on_target AND mc.away_shots_on_target>=b10.away_shots_on_target THEN (mc.home_shots_on_target-b10.home_shots_on_target)-(mc.away_shots_on_target-b10.away_shots_on_target) END AS dif_shots_on_target_last_10m,
    CASE WHEN mc.home_shots_insidebox>=b10.home_shots_insidebox THEN mc.home_shots_insidebox-b10.home_shots_insidebox END AS home_shots_insidebox_last_10m,
    CASE WHEN mc.away_shots_insidebox>=b10.away_shots_insidebox THEN mc.away_shots_insidebox-b10.away_shots_insidebox END AS away_shots_insidebox_last_10m,
    CASE WHEN mc.home_shots_insidebox>=b10.home_shots_insidebox AND mc.away_shots_insidebox>=b10.away_shots_insidebox THEN (mc.home_shots_insidebox-b10.home_shots_insidebox)-(mc.away_shots_insidebox-b10.away_shots_insidebox) END AS dif_shots_insidebox_last_10m,
    CASE WHEN mc.home_dangerous_attacks>=b10.home_dangerous_attacks THEN mc.home_dangerous_attacks-b10.home_dangerous_attacks END AS home_dangerous_attacks_last_10m,
    CASE WHEN mc.away_dangerous_attacks>=b10.away_dangerous_attacks THEN mc.away_dangerous_attacks-b10.away_dangerous_attacks END AS away_dangerous_attacks_last_10m,
    CASE WHEN mc.home_dangerous_attacks>=b10.home_dangerous_attacks AND mc.away_dangerous_attacks>=b10.away_dangerous_attacks THEN (mc.home_dangerous_attacks-b10.home_dangerous_attacks)-(mc.away_dangerous_attacks-b10.away_dangerous_attacks) END AS dif_dangerous_attacks_last_10m,
    CASE WHEN mc.home_corners>=b10.home_corners THEN mc.home_corners-b10.home_corners END AS home_corners_last_10m,
    CASE WHEN mc.away_corners>=b10.away_corners THEN mc.away_corners-b10.away_corners END AS away_corners_last_10m,
    CASE WHEN mc.home_corners>=b10.home_corners AND mc.away_corners>=b10.away_corners THEN (mc.home_corners-b10.home_corners)-(mc.away_corners-b10.away_corners) END AS dif_corners_last_10m,
    CASE WHEN mc.home_score<mc.away_score AND mc.home_shots_on_target>=b10.home_shots_on_target THEN mc.home_shots_on_target-b10.home_shots_on_target WHEN mc.away_score<mc.home_score AND mc.away_shots_on_target>=b10.away_shots_on_target THEN mc.away_shots_on_target-b10.away_shots_on_target END AS trailing_team_shots_on_target_last_10m,
    ev.available AS events_snapshot_available,
    ev.goals_70 AS goals_observed_since_70,
    ev.goals_10 AS goals_observed_last_10m,
    mc.snapshot_minute-ev.last_goal_minute AS minutes_since_last_goal_since_70,
    ev.var_10 AS var_events_last_10m,
    ev.home_subs_70 AS home_substitutions_since_70,
    ev.home_subs_10 AS home_substitutions_last_10m,
    ev.home_reds_70 AS home_dismissals_since_70,
    mc.snapshot_minute-ev.home_last_red AS minutes_since_home_dismissal_since_70,
    ev.away_subs_70 AS away_substitutions_since_70,
    ev.away_subs_10 AS away_substitutions_last_10m,
    ev.away_reds_70 AS away_dismissals_since_70,
    mc.snapshot_minute-ev.away_last_red AS minutes_since_away_dismissal_since_70,
    GREATEST(90-mc.snapshot_minute,0) AS minutes_to_90,
    mc.second_half_time_added AS second_half_time_added,
    CASE WHEN mc.state_developer_name='FT' THEN 0 WHEN mc.second_half_time_added IS NOT NULL THEN GREATEST(90+mc.second_half_time_added-mc.snapshot_minute,0) END AS announced_minutes_remaining,
    COALESCE(odds.valid,FALSE) AS has_valid_odds,
    EXTRACT(EPOCH FROM mc.captured_at-o.captured_at) AS odds_age_seconds
FROM normalized mc
JOIN fixtures fx ON fx.id=mc.fixture_id
JOIN match_centre_snapshots src ON src.id=mc.id
LEFT JOIN leagues lg ON lg.id=mc.league_id
LEFT JOIN teams ht ON ht.id=mc.home_team_id
LEFT JOIN teams at ON at.id=mc.away_team_id
CROSS JOIN params cfg
LEFT JOIN cluster_mapping cm ON cm.league_id=mc.league_id
LEFT JOIN LATERAL (
 SELECT h.* FROM h2h_corrected h
 WHERE h.fixture_id=mc.fixture_id AND h.home_team_id=mc.home_team_id AND h.away_team_id=mc.away_team_id
   AND h.captured_at<=mc.captured_at ORDER BY h.captured_at DESC,h.id DESC LIMIT 1
) hh ON TRUE
LEFT JOIN LATERAL (
 SELECT s.id,s.captured_at,s.games_played,s.position,s.points,s.goal_diff,s.goals_against,s.goals_for,s.wins,s.draws,s.losses
 FROM standings_snapshots s
 WHERE s.league_id=mc.league_id AND s.season_id=mc.season_id AND s.team_id=mc.home_team_id
   AND s.captured_at<=LEAST(mc.captured_at,fx.starting_at)
   AND NOT (COALESCE(s.fixture_id=mc.fixture_id,FALSE) AND s.capture_phase='postmatch')
   AND (mc.stage_id IS NULL OR s.raw_row->>'stage_id' IS NULL OR s.raw_row->>'stage_id'=mc.stage_id::text)
 ORDER BY s.captured_at DESC,s.id DESC LIMIT 1
) sh ON TRUE
LEFT JOIN LATERAL (
 SELECT s.id,s.captured_at,s.games_played,s.position,s.points,s.goal_diff,s.goals_against,s.goals_for,s.wins,s.draws,s.losses
 FROM standings_snapshots s
 WHERE s.league_id=mc.league_id AND s.season_id=mc.season_id AND s.team_id=mc.away_team_id
   AND s.captured_at<=LEAST(mc.captured_at,fx.starting_at)
   AND NOT (COALESCE(s.fixture_id=mc.fixture_id,FALSE) AND s.capture_phase='postmatch')
   AND (mc.stage_id IS NULL OR s.raw_row->>'stage_id' IS NULL OR s.raw_row->>'stage_id'=mc.stage_id::text)
 ORDER BY s.captured_at DESC,s.id DESC LIMIT 1
) sa ON TRUE
LEFT JOIN LATERAL (
 SELECT o.* FROM odds_snapshots o
 WHERE o.fixture_id=mc.fixture_id AND o.bookmaker_id=2 AND o.market_id=1 AND o.odds_type='inplay'
   AND o.snapshot_minute=mc.snapshot_minute AND o.captured_at<=mc.captured_at
   AND o.captured_at>=mc.captured_at-make_interval(secs=>cfg.max_odds_age_seconds)
 ORDER BY o.captured_at DESC,o.id DESC LIMIT 1
) o ON TRUE
LEFT JOIN LATERAL (
 SELECT COALESCE(o.odds_home>1 AND o.odds_draw>1 AND o.odds_away>1,FALSE)
        AND NOT EXISTS (
          SELECT 1 FROM jsonb_array_elements(CASE WHEN jsonb_typeof(o.raw_payload)='array' THEN o.raw_payload ELSE '[]'::jsonb END) r
          WHERE r->>'stopped'='true' OR r->>'suspended'='true'
        ) AS valid
) odds ON TRUE
LEFT JOIN LATERAL (
 SELECT EXISTS(SELECT 1 FROM match_centre_snapshots p WHERE p.fixture_id=mc.fixture_id AND p.state_developer_name='INPLAY_2ND_HALF' AND p.snapshot_minute>=GREATEST(mc.snapshot_minute-10,cfg.history_minute) AND p.snapshot_minute<mc.snapshot_minute AND p.captured_at<mc.captured_at AND p.home_score IS NOT NULL AND p.home_score<mc.home_score) AS home_score_increased,
        EXISTS(SELECT 1 FROM match_centre_snapshots p WHERE p.fixture_id=mc.fixture_id AND p.state_developer_name='INPLAY_2ND_HALF' AND p.snapshot_minute>=GREATEST(mc.snapshot_minute-10,cfg.history_minute) AND p.snapshot_minute<mc.snapshot_minute AND p.captured_at<mc.captured_at AND p.away_score IS NOT NULL AND p.away_score<mc.away_score) AS away_score_increased,
        EXISTS(SELECT 1 FROM match_centre_snapshots p WHERE p.fixture_id=mc.fixture_id AND p.state_developer_name='INPLAY_2ND_HALF' AND p.snapshot_minute>=GREATEST(mc.snapshot_minute-10,cfg.history_minute) AND p.snapshot_minute<mc.snapshot_minute AND p.captured_at<mc.captured_at AND p.home_score IS NOT NULL AND p.away_score IS NOT NULL) AS has_score_history
) prev ON TRUE
LEFT JOIN LATERAL (
 SELECT p.id FROM match_centre_snapshots p
 WHERE p.fixture_id=mc.fixture_id AND p.snapshot_minute=mc.snapshot_minute-5
   AND p.home_team_id=mc.home_team_id AND p.away_team_id=mc.away_team_id
   AND p.state_developer_name='INPLAY_2ND_HALF' AND p.captured_at<mc.captured_at
 ORDER BY p.captured_at DESC,p.id DESC LIMIT 1
) b5_id ON TRUE
LEFT JOIN normalized b5 ON b5.id=b5_id.id
LEFT JOIN LATERAL (
 SELECT p.id FROM match_centre_snapshots p
 WHERE p.fixture_id=mc.fixture_id AND p.snapshot_minute=mc.snapshot_minute-10
   AND p.home_team_id=mc.home_team_id AND p.away_team_id=mc.away_team_id
   AND p.state_developer_name='INPLAY_2ND_HALF' AND p.captured_at<mc.captured_at
 ORDER BY p.captured_at DESC,p.id DESC LIMIT 1
) b10_id ON TRUE
LEFT JOIN normalized b10 ON b10.id=b10_id.id
LEFT JOIN LATERAL (
 SELECT COALESCE(jsonb_typeof(mc.raw_events)='array' AND mc.second_half_period_id IS NOT NULL,FALSE) AS available,
        CASE WHEN jsonb_typeof(mc.raw_events)='array' AND mc.second_half_period_id IS NOT NULL THEN a.goals_70 END AS goals_70,
        CASE WHEN jsonb_typeof(mc.raw_events)='array' AND mc.second_half_period_id IS NOT NULL THEN a.goals_10 END AS goals_10,
        CASE WHEN jsonb_typeof(mc.raw_events)='array' AND mc.second_half_period_id IS NOT NULL THEN a.last_goal_minute END AS last_goal_minute,
        CASE WHEN jsonb_typeof(mc.raw_events)='array' AND mc.second_half_period_id IS NOT NULL THEN a.var_10 END AS var_10,
        CASE WHEN jsonb_typeof(mc.raw_events)='array' AND mc.second_half_period_id IS NOT NULL THEN a.home_subs_70 END AS home_subs_70,
        CASE WHEN jsonb_typeof(mc.raw_events)='array' AND mc.second_half_period_id IS NOT NULL THEN a.home_subs_10 END AS home_subs_10,
        CASE WHEN jsonb_typeof(mc.raw_events)='array' AND mc.second_half_period_id IS NOT NULL THEN a.home_reds_70 END AS home_reds_70,
        CASE WHEN jsonb_typeof(mc.raw_events)='array' AND mc.second_half_period_id IS NOT NULL THEN a.home_last_red END AS home_last_red,
        CASE WHEN jsonb_typeof(mc.raw_events)='array' AND mc.second_half_period_id IS NOT NULL THEN a.away_subs_70 END AS away_subs_70,
        CASE WHEN jsonb_typeof(mc.raw_events)='array' AND mc.second_half_period_id IS NOT NULL THEN a.away_subs_10 END AS away_subs_10,
        CASE WHEN jsonb_typeof(mc.raw_events)='array' AND mc.second_half_period_id IS NOT NULL THEN a.away_reds_70 END AS away_reds_70,
        CASE WHEN jsonb_typeof(mc.raw_events)='array' AND mc.second_half_period_id IS NOT NULL THEN a.away_last_red END AS away_last_red
 FROM (
   SELECT COUNT(*) FILTER(WHERE e.type_id IN(14,15,16)) AS goals_70, COUNT(*) FILTER(WHERE e.type_id IN(14,15,16) AND e.elapsed_minute>mc.snapshot_minute-10) AS goals_10, MAX(e.elapsed_minute) FILTER(WHERE e.type_id IN(14,15,16)) AS last_goal_minute, COUNT(*) FILTER(WHERE e.type_id IN(10,1697) AND e.elapsed_minute>mc.snapshot_minute-10) AS var_10, COUNT(*) FILTER(WHERE e.type_id=18 AND e.team_id=mc.home_team_id) AS home_subs_70, COUNT(*) FILTER(WHERE e.type_id=18 AND e.team_id=mc.home_team_id AND e.elapsed_minute>mc.snapshot_minute-10) AS home_subs_10, COUNT(*) FILTER(WHERE e.type_id IN(20,21) AND e.team_id=mc.home_team_id) AS home_reds_70, MAX(e.elapsed_minute) FILTER(WHERE e.type_id IN(20,21) AND e.team_id=mc.home_team_id) AS home_last_red, COUNT(*) FILTER(WHERE e.type_id=18 AND e.team_id=mc.away_team_id) AS away_subs_70, COUNT(*) FILTER(WHERE e.type_id=18 AND e.team_id=mc.away_team_id AND e.elapsed_minute>mc.snapshot_minute-10) AS away_subs_10, COUNT(*) FILTER(WHERE e.type_id IN(20,21) AND e.team_id=mc.away_team_id) AS away_reds_70, MAX(e.elapsed_minute) FILTER(WHERE e.type_id IN(20,21) AND e.team_id=mc.away_team_id) AS away_last_red
   FROM (
     SELECT DISTINCT ON (r->>'id')
        (r->>'type_id')::integer AS type_id,
        COALESCE(r->>'participant_id',r->>'team_id')::bigint AS team_id,
        (r->>'minute')::integer+COALESCE((r->>'extra_minute')::integer,0) AS elapsed_minute
     FROM jsonb_array_elements(CASE WHEN jsonb_typeof(mc.raw_events)='array' THEN mc.raw_events ELSE '[]'::jsonb END) r
     WHERE r->>'period_id'=mc.second_half_period_id::text
       AND r->>'type_id' NOT IN('22','23')
       AND r->>'rescinded' IS DISTINCT FROM 'true'
       AND r->>'on_bench' IS DISTINCT FROM 'true'
       AND (r->>'minute')::integer>=70
       AND (r->>'minute')::integer+COALESCE((r->>'extra_minute')::integer,0)<=mc.snapshot_minute
   ) e
 ) a
) ev ON TRUE
WHERE mc.id=ANY($1::bigint[]) AND mc.snapshot_minute>=70
ORDER BY fx.starting_at DESC,mc.fixture_id DESC,mc.captured_at DESC,mc.id DESC
