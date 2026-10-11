"""Daily isolated fixture-link builder. Provider outages never reach inference."""
import argparse
import asyncio
from datetime import date, datetime, timedelta, timezone
import logging
from zoneinfo import ZoneInfo
import asyncpg
import httpx
from integration.config import ROOT,bounded,secret
from integration.database import pool,transaction
from integration.fixture_links import PROVIDERS,day_bounds,event_index,resolve,validate_events
from integration.link_catalogue import download
from integration.link_schedule import configuration,due_day

log = logging.getLogger(__name__)
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
SQL = (ROOT/'sql'/'source_daily_fixtures.sql').read_text()


async def source_fixtures(day):
    dsn = secret('V6_SOURCE_DATABASE_URL')
    if not dsn:raise ValueError('readonly_source_not_configured')
    limit = int(bounded('V6_LINK_MAX_FIXTURES',2500,1,10000))
    source = await asyncpg.connect(dsn,timeout=3,command_timeout=2,server_settings={
        'application_name':'sportedge_v6_daily_links_readonly','default_transaction_read_only':'on',
        'statement_timeout':'1500','lock_timeout':'100','idle_in_transaction_session_timeout':'5000','jit':'off'})
    try:
        async with source.transaction(readonly=True):
            rows = await source.fetch(SQL,*day_bounds(day),limit+1)
            if len(rows)>limit:raise ValueError('daily_fixture_limit_exceeded')
            return [dict(row) for row in rows]
    finally:
        await source.close() # Never occupies a source connection while downloading.


async def save_registry(p,day,provider,fixtures,events=None,error=None,origin=None):
    index = event_index(events or [])
    async with transaction(p) as c:
        aliases = {(r['entity'],r['source_id']):r['provider_name'] for r in await c.fetch(
            'SELECT entity,source_id,provider_name FROM v6.fixture_link_aliases WHERE provider=$1',provider)}
        resolutions = [{'status':'provider_error','evidence':{'reason':error}} if error else resolve(fixture,index,aliases) for fixture in fixtures]
        event_counts = {}
        for resolution in resolutions:
            if resolution['status']=='matched':
                event_counts[resolution['event_id']]=event_counts.get(resolution['event_id'],0)+1
        for resolution in resolutions:
            if resolution['status']=='matched' and event_counts[resolution['event_id']]>1:
                resolution.update(status='ambiguous',url=None,evidence={'reason':'event_matches_multiple_source_fixtures','candidate_ids':[resolution['event_id']]})
        matched = 0
        valid_until = day_bounds(day)[1]+timedelta(hours=12)
        # A successful new catalogue invalidates links for removed/rescheduled fixtures.
        if error is None:
            await c.execute('DELETE FROM v6.fixture_link_registry WHERE match_day=$1 AND provider=$2',day,provider)
        for fixture,resolution in zip(fixtures,resolutions):
            evidence = resolution['evidence'] | {'catalogue_origin':origin}
            changed = await c.fetchval('''INSERT INTO v6.fixture_link_registry
                (fixture_id,match_day,provider,starting_at,home_team_id,away_team_id,league_id,status,event_id,url,evidence,valid_until)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                ON CONFLICT(fixture_id,match_day,provider) DO UPDATE SET starting_at=EXCLUDED.starting_at,
                home_team_id=EXCLUDED.home_team_id,away_team_id=EXCLUDED.away_team_id,league_id=EXCLUDED.league_id,
                status=EXCLUDED.status,event_id=EXCLUDED.event_id,url=EXCLUDED.url,evidence=EXCLUDED.evidence,
                valid_until=EXCLUDED.valid_until,resolved_at=now()
                WHERE NOT ($13 AND v6.fixture_link_registry.status='matched'
                    AND v6.fixture_link_registry.valid_until>now()
                    AND v6.fixture_link_registry.starting_at=EXCLUDED.starting_at
                    AND v6.fixture_link_registry.home_team_id=EXCLUDED.home_team_id
                    AND v6.fixture_link_registry.away_team_id=EXCLUDED.away_team_id
                    AND v6.fixture_link_registry.league_id=EXCLUDED.league_id)
                RETURNING status''',fixture['fixture_id'],day,provider,fixture['starting_at'],fixture['home_team_id'],
                fixture['away_team_id'],fixture['league_id'],resolution['status'],resolution.get('event_id'),resolution.get('url'),
                evidence,valid_until,bool(error))
            matched += int(changed=='matched')
        if error:
            matched = await c.fetchval("SELECT count(*) FROM v6.fixture_link_registry WHERE match_day=$1 AND provider=$2 AND status='matched' AND valid_until>now()",day,provider)
        refresh = int(bounded('V6_LINK_REFRESH_SECONDS',21600,900,86400))
        await c.execute('''UPDATE v6.fixture_link_runs SET status=$3,finished_at=now(),last_error=$4,
            fixture_count=$5,matched_count=$6,next_attempt_at=now()+make_interval(secs=>$7),
            attempts=CASE WHEN $3='completed' THEN 0 ELSE attempts END
            WHERE match_day=$1 AND provider=$2''',day,provider,'failed' if error else 'completed',error,
            len(fixtures),matched,900 if error else refresh)
    return matched


async def build_day(p,client,day,force=False):
    # Shared lock also serializes imported catalogues against a running build.
    async with p.acquire() as lock:
        if not await lock.fetchval('SELECT pg_try_advisory_lock(610104)'):
            return {'status':'busy'}
        try:
            async with p.acquire() as c:
                runs = {r['provider']:r for r in await c.fetch('SELECT * FROM v6.fixture_link_runs WHERE match_day=$1',day)}
            now = datetime.now(timezone.utc)
            providers = [provider for provider in PROVIDERS if force or provider not in runs
                or (runs[provider]['next_attempt_at']<=now and (runs[provider]['status']!='failed' or runs[provider]['attempts']<4))]
            if not providers:return {'status':'not_due'}
            fixtures = None
            source_error = None
            try:fixtures = await source_fixtures(day)
            except Exception as exc:source_error = 'source_'+type(exc).__name__
            summary = {}
            for provider in providers:
                async with p.acquire() as c:
                    await c.execute('''INSERT INTO v6.fixture_link_runs(match_day,provider,status)
                        VALUES ($1,$2,'running') ON CONFLICT(match_day,provider) DO UPDATE SET status='running',
                        attempts=v6.fixture_link_runs.attempts+1,started_at=now(),finished_at=NULL,
                        next_attempt_at=now()+interval '15 minutes' ''',day,provider)
                events = None
                origin = None
                error = source_error
                if not error:
                    try:
                        async with p.acquire() as c:
                            catalogue = await c.fetchrow('''SELECT events,origin FROM v6.fixture_link_catalogues
                                WHERE match_day=$1 AND provider=$2 AND valid_until>now()''',day,provider)
                        if catalogue:
                            events = validate_events(catalogue['events'],day,provider=provider)
                            origin = catalogue['origin']
                        else:
                            events = await asyncio.wait_for(download(client,provider,day,'America/Lima'),20)
                            origin = 'bulk_download'
                    except Exception as exc:
                        error = str(exc) if isinstance(exc,ValueError) else 'catalogue_'+type(exc).__name__
                        error = error[:120]
                matched = await save_registry(p,day,provider,fixtures or [],events,error,origin)
                summary[provider] = {'fixtures':len(fixtures or []),'matched':matched,'error':error}
                log.info('Daily links %s %s: %s',day,provider,summary[provider])
            return summary
        finally:
            await lock.execute('SELECT pg_advisory_unlock(610104)')


async def run(once=False,day=None,force=False):
    log.info('V6 fixture catalogues started: service=links-worker configuration=%s',configuration())
    p = await pool()
    if p.get_max_size()<2:raise RuntimeError('Link worker requires V6_DB_POOL_SIZE >= 2')
    try:
        # IPv4 avoids the observed read timeout on the provider's IPv6 route
        # from Docker Desktop. TLS verification and hostname checks remain on.
        transport = httpx.AsyncHTTPTransport(local_address='0.0.0.0',
            limits=httpx.Limits(max_connections=1,max_keepalive_connections=1))
        async with httpx.AsyncClient(transport=transport) as client:
            while True:
                target = day or due_day()
                try:
                    if target:await build_day(p,client,target,force)
                    if not once:
                        async with p.acquire() as c:
                            requested = await c.fetchval('''SELECT match_day FROM v6.fixture_link_runs
                                WHERE status='failed' AND attempts=0 AND next_attempt_at<=now()
                                AND match_day<=$1 AND last_error IN ('rebuild_requested','catalogue_imported_rebuild_pending')
                                ORDER BY match_day DESC LIMIT 1''',datetime.now(timezone.utc).astimezone(ZoneInfo('America/Lima')).date())
                        if requested and requested!=target:await build_day(p,client,requested)
                except Exception as exc:
                    log.error('Daily links interrupted: %s',type(exc).__name__)
                    if once:raise
                if once:return
                await asyncio.sleep(60)
    finally:await p.close()


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--date',type=date.fromisoformat)
    parser.add_argument('--force',action='store_true')
    args = parser.parse_args()
    asyncio.run(run(args.once,args.date,args.force))
