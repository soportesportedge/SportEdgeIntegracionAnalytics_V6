"""Batch selection and durable requests for the separate Betano browser."""
import uuid
from integration.fixture_links import instant, valid_event_url
from integration.link_schedule import incremental_window

IDENTITY = ('starting_at','home_team_id','away_team_id','league_id')


def same_fixture(row, fixture):
    return (row['starting_at'] == instant(fixture['starting_at'])
            and all(row[k] == fixture[k] for k in IDENTITY[1:]))


def valid_cached(row, fixture, now):
    return bool(row and row['status']=='matched' and row['valid_until']>now
                and same_fixture(row,fixture)
                and valid_event_url(row['provider'],row['url'],row['event_id']))


def select_fixtures(fixtures, rows, mode, now):
    if mode=='full':
        return list(fixtures)
    start,end=incremental_window(now)
    cached={r['fixture_id']:r for r in rows}
    return [f for f in fixtures if start<=instant(f['starting_at'])<=end
            and not valid_cached(cached.get(f['fixture_id']),f,now)]


async def request_betano(c, day, slot):
    request=await c.fetchrow("SELECT * FROM v6.fixture_link_catalogue_requests WHERE match_day=$1 AND provider='betano'",day)
    if request and request['slot_at']==slot:
        if request['status']=='failed' and request['attempts']>=4:
            raise ValueError('betano_browser_retries_exhausted')
        return
    await c.execute('''INSERT INTO v6.fixture_link_catalogue_requests
        (match_day,provider,request_id,slot_at,status)
        VALUES ($1,'betano',$2,$3,'pending')
        ON CONFLICT(match_day,provider) DO UPDATE SET request_id=EXCLUDED.request_id,
        slot_at=EXCLUDED.slot_at,status='pending',attempts=0,requested_at=now(),
        started_at=NULL,finished_at=NULL,next_attempt_at=now(),event_count=0,last_error=NULL''',day,uuid.uuid4(),slot)


async def claim_browser_request(p):
    async with p.acquire() as c,c.transaction():
        return await c.fetchrow('''WITH candidate AS (
            SELECT match_day,provider FROM v6.fixture_link_catalogue_requests
            WHERE ((status IN ('pending','failed') AND attempts<4 AND next_attempt_at<=now())
                OR (status='running' AND started_at<now()-interval '5 minutes'))
            ORDER BY requested_at FOR UPDATE SKIP LOCKED LIMIT 1)
            UPDATE v6.fixture_link_catalogue_requests r SET status='running',
            attempts=r.attempts+1,started_at=now(),finished_at=NULL
            FROM candidate c WHERE r.match_day=c.match_day AND r.provider=c.provider RETURNING r.*''')


async def fail_browser_request(p, request, reason):
    async with p.acquire() as c:
        await c.execute('''UPDATE v6.fixture_link_catalogue_requests SET status='failed',
            finished_at=now(),next_attempt_at=now()+interval '15 minutes',last_error=$2
            WHERE request_id=$1 AND status='running' ''',request['request_id'],reason[:120])
