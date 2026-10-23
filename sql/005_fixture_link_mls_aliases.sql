-- Verified provider names for the MLS fixtures affected on 2026-10-14.
-- Serializes with catalogue builds; operator-managed aliases take precedence.
SELECT pg_advisory_xact_lock(610104);
INSERT INTO v6.fixture_link_aliases (provider, entity, source_id, provider_name)
VALUES
    ('365scores', 'league', 779, 'MLS'),
    ('betano', 'league', 779, 'MLS'),
    ('365scores', 'team', 3627, 'New York City FC'),
    ('365scores', 'team', 111, 'Toronto FC'),
    ('365scores', 'team', 204, 'Orlando City SC'),
    ('365scores', 'team', 3636, 'FC Cincinnati'),
    ('365scores', 'team', 641, 'New England Revolution'),
    ('betano', 'team', 239235, 'Inter Miami CF'),
    ('betano', 'team', 3627, 'New York City FC'),
    ('betano', 'team', 111, 'Toronto FC'),
    ('betano', 'team', 204, 'Orlando City SC'),
    ('betano', 'team', 3636, 'FC Cincinnati'),
    ('betano', 'team', 641, 'New England Revolution')
ON CONFLICT (provider, entity, source_id) DO NOTHING;
