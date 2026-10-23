"""Regression for provider-name mismatches observed in the 2026-10-14 alerts."""
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from integration.database import pool
from integration.fixture_links import cached_links, day_bounds, with_sportedge_link
from integration import link_worker, telegram

MIGRATION = (ROOT / 'sql' / '005_fixture_link_mls_aliases.sql').read_text()
FIXTURES = [
    (19609874, 239235, 3627, 'Inter Miami', 'New York City'),
    (19609880, 111, 204, 'Toronto', 'Orlando City'),
    (19609878, 3636, 641, 'Cincinnati', 'New England'),
]
PROVIDER_NAMES = {
    '365scores': [('Inter Miami', 'New York City FC'), ('Toronto FC', 'Orlando City SC'),
                  ('FC Cincinnati', 'New England Revolution')],
    'betano': [('Inter Miami CF', 'New York City FC'), ('Toronto FC', 'Orlando City SC'),
               ('FC Cincinnati', 'New England Revolution')],
}


def test_mls_aliases_fix_batch_registry_and_telegram_without_provider_requests():
    assert os.environ['V6_DATABASE_URL'].endswith('/sportedge_v6_test')

    async def case():
        p = await pool()
        today = datetime.now(ZoneInfo('America/Lima')).date()
        kickoff = day_bounds(today)[0].replace(hour=23, minute=30)
        fixtures = [dict(fixture_id=i, home_team_id=h, away_team_id=a, home=hn, away=an,
                         league_id=779, league='Major League Soccer', country=None,
                         starting_at=kickoff) for i, h, a, hn, an in FIXTURES]
        try:
            async with p.acquire() as c:
                await c.execute('TRUNCATE v6.fixture_link_registry,v6.fixture_link_runs,v6.fixture_link_aliases')
                # Reproduce the original configuration: league aliases, no team aliases.
                for provider in PROVIDER_NAMES:
                    await c.execute("INSERT INTO v6.fixture_link_aliases VALUES ($1,'league',779,'MLS',now())", provider)
                    await c.execute("INSERT INTO v6.fixture_link_runs(match_day,provider,status) VALUES ($1,$2,'running')", today, provider)
            events = {}
            for provider, names in PROVIDER_NAMES.items():
                events[provider] = []
                for fixture, (home, away) in zip(fixtures, names):
                    event_id = str(fixture['fixture_id'])
                    url = ('https://www.365scores.com/es/football/match/mls-104/home-away-1-2-104#id=' + event_id
                           if provider == '365scores' else
                           'https://www.betano.pe/cuotas-de-partido/home-away/' + event_id + '/')
                    events[provider].append(dict(event_id=event_id, home=home, away=away, league='MLS',
                                                 country='USA', starting_at=kickoff.isoformat(), url=url))
                assert await link_worker.save_registry(p, today, provider, fixtures, events[provider]) == 0
            async with p.acquire() as c:
                await c.execute(MIGRATION)
            for provider in PROVIDER_NAMES:
                assert await link_worker.save_registry(p, today, provider, fixtures, events[provider]) == 3
            async with p.acquire() as c:
                for fixture in fixtures:
                    snapshot = fixture | {'id': fixture['fixture_id'], 'starting_at': kickoff.isoformat()}
                    links = await cached_links(c, snapshot)
                    assert set(links) == {'365scores', 'betano'}
                    payload = {'snapshot': snapshot, 'prediction': {},
                               'fixture_links': with_sportedge_link(snapshot, links)}
                    before = json.dumps(snapshot, sort_keys=True)
                    text = telegram.format_message(payload)
                    assert text.count('<a href=') == 3
                    assert all(link['url'] in text for link in payload['fixture_links'].values())
                    assert json.dumps(snapshot, sort_keys=True) == before
                    assert await cached_links(c, snapshot | {'away_team_id': 999999}) == {}
        finally:
            await p.close()

    asyncio.run(case())


def test_mls_seed_is_repeatable_and_preserves_operator_aliases():
    assert os.environ['V6_DATABASE_URL'].endswith('/sportedge_v6_test')

    async def case():
        p = await pool()
        try:
            async with p.acquire() as c, c.transaction():
                await c.execute('TRUNCATE v6.fixture_link_aliases')
                await c.execute("INSERT INTO v6.fixture_link_aliases VALUES ('betano','team',111,'Operator Toronto',now())")
                await c.execute(MIGRATION)
                before = await c.fetch('SELECT * FROM v6.fixture_link_aliases ORDER BY provider,entity,source_id')
                await c.execute(MIGRATION)
                assert await c.fetch('SELECT * FROM v6.fixture_link_aliases ORDER BY provider,entity,source_id') == before
                assert len(before) == 13
                assert await c.fetchval("SELECT provider_name FROM v6.fixture_link_aliases WHERE provider='betano' AND entity='team' AND source_id=111") == 'Operator Toronto'
        finally:
            await p.close()

    asyncio.run(case())
