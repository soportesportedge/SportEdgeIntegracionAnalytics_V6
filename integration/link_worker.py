"""Daily isolated fixture-link builder. Provider outages never reach inference."""
import argparse
import asyncio
from datetime import date, datetime, timedelta, timezone
import logging
import os
from zoneinfo import ZoneInfo
import asyncpg
import httpx
from integration.config import ROOT,bounded,secret
from integration.database import pool,transaction
from integration.fixture_links import PROVIDERS,day_bounds,event_index,resolve,validate_events
from integration.link_catalogue import download,DEFAULT_URLS
from integration.link_schedule import configuration,due_day,daily_start_at,slot_at,next_slot,incremental_window,scheduled_days
from integration.link_jobs import select_fixtures,request_betano

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


async def save_registry(p,day,provider,fixtures,events=None,error=None,origin=None,*,
                        mode='full',now=None,slot=None,total_fixtures=None,pending=False):
    now=now or datetime.now(timezone.utc)
    slot=slot or slot_at(now)
    total_fixtures=fixtures if total_fixtures is None else total_fixtures
    index = event_index(events or [])
    async with transaction(p) as c:
        aliases = {(r['entity'],r['source_id']):r['provider_name'] for r in await c.fetch(
            'SELECT entity,source_id,provider_name FROM v6.fixture_link_aliases WHERE provider=$1',provider)}
        resolutions = [] if pending else [{'status':'provider_error','evidence':{'reason':error}}
                        if error else resolve(fixture,index,aliases) for fixture in fixtures]
        event_counts = {}
        for resolution in resolutions:
            if resolution['status']=='matched':
                event_counts[resolution['event_id']]=event_counts.get(resolution['event_id'],0)+1
        for resolution in resolutions:
            if resolution['status']=='matched' and event_counts[resolution['event_id']]>1:
                resolution.update(status='ambiguous',url=None,evidence={'reason':'event_matches_multiple_source_fixtures','candidate_ids':[resolution['event_id']]})
        matched = 0
        valid_until = day_bounds(day)[1]+timedelta(hours=12)
        # Neither incremental downloads nor partial featured catalogues erase links.
        existing={r['fixture_id']:r for r in await c.fetch(
            'SELECT * FROM v6.fixture_link_registry WHERE match_day=$1 AND provider=$2',day,provider)}
        owners={r['event_id']:r['fixture_id'] for r in existing.values() if r['status']=='matched'}
        for fixture,resolution in zip(fixtures,resolutions):
            if (resolution['status']=='matched' and resolution['event_id'] in owners
                    and owners[resolution['event_id']]!=fixture['fixture_id']):
                resolution.update(status='ambiguous',url=None,evidence={
                    'reason':'event_already_linked_to_other_fixture','candidate_ids':[resolution['event_id']]})
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
                evidence,valid_until,resolution['status']!='matched')
            matched += int(changed=='matched' or (changed is None and fixture['fixture_id'] in existing
                            and existing[fixture['fixture_id']]['status']=='matched'))
        total_matched=await c.fetchval("SELECT count(*) FROM v6.fixture_link_registry WHERE match_day=$1 AND provider=$2 AND status='matched' AND valid_until>now()",day,provider)
        retry_at=now+timedelta(seconds=60 if pending else 900)
        next_at=min(retry_at,next_slot(now)) if error or pending else next_slot(now)
        window_start,window_end=incremental_window(now) if mode=='incremental' else (None,None)
        selected_ids={f['fixture_id'] for f in fixtures}
        await c.execute('''UPDATE v6.fixture_link_runs SET status=$3,finished_at=now(),last_error=$4,
            fixture_count=$5,matched_count=$6,next_attempt_at=$7,
            attempts=CASE WHEN $3='completed' OR $8 THEN 0 ELSE attempts END,
            mode=$9,slot_at=$10,window_start=$11,window_end=$12,candidate_count=$13,
            preserved_count=$14,full_completed_at=CASE WHEN $9='full' AND $3='completed'
                THEN now() ELSE full_completed_at END
            WHERE match_day=$1 AND provider=$2''',day,provider,'failed' if error or pending else 'completed',
            'catalogue_refresh_pending' if pending else error,len(total_fixtures),total_matched,next_at,pending,
            mode,slot,window_start,window_end,len(fixtures),
            sum(1 for r in existing.values() if r['status']=='matched' and r['valid_until']>now
                and r['fixture_id'] not in selected_ids))
    return matched


async def catalogue(p,client,provider,day,slot,refresh=False):
    async with p.acquire() as c:
        saved=await c.fetchrow('''SELECT events,origin,imported_at FROM v6.fixture_link_catalogues
            WHERE match_day=$1 AND provider=$2 AND valid_until>now()''',day,provider)
        native_origin=saved and saved['origin'] in ('bulk_download','betano_public_browser')
        if saved and (not native_origin or (saved['imported_at']>=slot and not refresh)):
            return validate_events(saved['events'],day,provider=provider),saved['origin']
        if provider=='betano' and os.getenv('V6_BETANO_CATALOGUE_URL',DEFAULT_URLS['betano'])==DEFAULT_URLS['betano']:
            await request_betano(c,day,slot)
            return None,'betano_public_browser_pending'
    events=await asyncio.wait_for(download(client,provider,day,'America/Lima'),20)
    # Native 365Scores is a daily bulk response, not an event-by-event API.
    if provider=='365scores':
        async with p.acquire() as c:
            await c.execute('''INSERT INTO v6.fixture_link_catalogues(match_day,provider,events,origin,valid_until)
                VALUES ($1,$2,$3,'bulk_download',$4) ON CONFLICT(match_day,provider) DO UPDATE
                SET events=EXCLUDED.events,origin=EXCLUDED.origin,valid_until=EXCLUDED.valid_until,imported_at=now()''',
                day,provider,events,day_bounds(day)[1]+timedelta(hours=12))
    return events,'bulk_download'


def is_due(run,now,slot,force,scheduled):
    if force or run is None:return True
    if run['last_error'] in ('rebuild_requested','catalogue_imported_rebuild_pending','catalogue_ready'):
        return run['next_attempt_at']<=now
    if scheduled and (run['slot_at'] is None or run['slot_at']<slot):return True
    return run['next_attempt_at']<=now and (run['status']!='failed' or run['attempts']<4)


async def build_day(p,client,day,force=False,*,now=None,scheduled=False):
    # Shared lock also serializes imported catalogues against a running build.
    async with p.acquire() as lock:
        if not await lock.fetchval('SELECT pg_try_advisory_lock(610104)'):
            return {'status':'busy'}
        try:
            async with p.acquire() as c:
                runs = {r['provider']:r for r in await c.fetch('SELECT * FROM v6.fixture_link_runs WHERE match_day=$1',day)}
            now = now or datetime.now(timezone.utc)
            slot=slot_at(now)
            providers = [provider for provider in PROVIDERS
                         if is_due(runs.get(provider),now,slot,force,scheduled)]
            waiting=runs.get('betano')
            if ('betano' in providers and not force and waiting
                    and waiting['last_error']=='catalogue_refresh_pending' and waiting['slot_at']==slot):
                async with p.acquire() as c:
                    request=await c.fetchrow("SELECT status,attempts FROM v6.fixture_link_catalogue_requests WHERE match_day=$1 AND provider='betano'",day)
                if request and (request['status'] in ('pending','running')
                                or (request['status']=='failed' and request['attempts']<4)):
                    providers.remove('betano')
            if not providers:return {'status':'not_due'}
            fixtures = None
            source_error = None
            try:fixtures = await source_fixtures(day)
            except Exception as exc:source_error = 'source_'+type(exc).__name__
            summary = {}
            for provider in providers:
                previous=runs.get(provider)
                requested=previous and previous['last_error'] in ('rebuild_requested','catalogue_imported_rebuild_pending')
                daily_complete=previous and previous['full_completed_at'] and previous['full_completed_at']>=daily_start_at(day)
                full=(force or requested or (not scheduled and previous is None)
                      or (day==due_day(now) and not daily_complete))
                mode='full' if full else 'incremental'
                async with p.acquire() as c:
                    rows=await c.fetch('SELECT * FROM v6.fixture_link_registry WHERE match_day=$1 AND provider=$2',day,provider)
                    selected=select_fixtures(fixtures or [],rows,mode,now)
                    await c.execute('''INSERT INTO v6.fixture_link_runs(match_day,provider,status)
                        VALUES ($1,$2,'running') ON CONFLICT(match_day,provider) DO UPDATE SET status='running',
                        attempts=CASE WHEN v6.fixture_link_runs.slot_at=$3 THEN v6.fixture_link_runs.attempts+1 ELSE 1 END,
                        started_at=now(),finished_at=NULL,next_attempt_at=now()+interval '15 minutes' ''',day,provider,slot)
                events = None
                origin = None
                error = source_error
                pending=False
                if not error and selected:
                    try:
                        events,origin=await catalogue(p,client,provider,day,slot,refresh=force and provider=='365scores')
                        pending=events is None
                    except Exception as exc:
                        error = str(exc) if isinstance(exc,ValueError) else 'catalogue_'+type(exc).__name__
                        error = error[:120]
                matched = await save_registry(p,day,provider,selected,events,error,origin,mode=mode,
                                              now=now,slot=slot,total_fixtures=fixtures or [],pending=pending)
                summary[provider] = {'mode':mode,'slot_at':slot.isoformat(),'fixtures':len(fixtures or []),
                    'candidates':len(selected),'matched':matched,'catalogue_pending':pending,'error':error}
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
                now=datetime.now(timezone.utc)
                targets=[day] if day else scheduled_days(now)
                try:
                    for target in targets:await build_day(p,client,target,force,now=now,scheduled=True)
                    if not once:
                        async with p.acquire() as c:
                            requested = await c.fetchval('''SELECT match_day FROM v6.fixture_link_runs
                                WHERE status='failed' AND attempts=0 AND next_attempt_at<=now()
                                AND match_day<=$1 AND last_error IN ('rebuild_requested','catalogue_imported_rebuild_pending')
                                ORDER BY match_day DESC LIMIT 1''',datetime.now(timezone.utc).astimezone(ZoneInfo('America/Lima')).date())
                        if requested and requested not in targets:await build_day(p,client,requested,scheduled=True)
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
