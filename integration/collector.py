"""Optional read-only sidecar. Never writes to or imports the current backend."""
import asyncio
from datetime import datetime
from decimal import Decimal
import json
import logging
import os
import asyncpg
from integration.config import ROOT, secret, enabled, bounded
from integration.database import pool,init_connection
from integration import repository as repo

log=logging.getLogger(__name__)
SQL=(ROOT/'sql'/'source_features.sql').read_text()

def serialise(v):
    if isinstance(v,datetime):return v.isoformat()
    if isinstance(v,Decimal):return float(v)
    raise TypeError(type(v).__name__)

async def tick(p, source):
    async with p.acquire() as c:
        if await c.fetchval("SELECT count(*) FROM v6.match_centre_invocations WHERE status IN ('pending','processing')") >= int(bounded('V6_MAX_BACKLOG',5000,1,100000))*.9:return 0
        cursor=await c.fetchval("SELECT last_id FROM v6.collector_cursor WHERE name='match_centre'")
    async with source.transaction(isolation='repeatable_read',readonly=True):
        if cursor is None:
            cursor=await source.fetchval('SELECT COALESCE(max(id),0) FROM match_centre_snapshots')
            async with p.acquire() as c:await c.execute("INSERT INTO v6.collector_cursor(name,last_id) VALUES ('match_centre',$1) ON CONFLICT DO NOTHING",cursor)
            return 0
        overlap=int(bounded('V6_SOURCE_OVERLAP_IDS',128,0,512));batch=int(bounded('V6_SOURCE_BATCH_SIZE',10,1,25))
        rows=await source.fetch('''SELECT id, fixture_id, captured_at, snapshot_minute, state_developer_name
            FROM match_centre_snapshots WHERE id>$1 ORDER BY id LIMIT $2''',max(0,cursor-overlap),batch+overlap)
        if not rows:return 0
        ids=[r['id'] for r in rows if r['state_developer_name']=='INPLAY_2ND_HALF'
             and r['snapshot_minute'] is not None and r['snapshot_minute']>=70
             and r['captured_at'].timestamp() >= datetime.now().timestamp()-bounded('V6_SOURCE_MAX_AGE_SECONDS',300,30,1800)]
        async with p.acquire() as c:
            known=await c.fetch('SELECT idempotency_key FROM v6.match_centre_invocations WHERE idempotency_key=ANY($1::text[])',['match_centre:'+str(x) for x in ids])
        existing={r['idempotency_key'] for r in known}
        remaining=[x for x in ids if 'match_centre:'+str(x) not in existing]
        ids=remaining[:batch]
        features=await source.fetch(SQL,ids) if ids else []
        feature_map={r['match_centre_snapshot_id']:dict(r) for r in features}
        # Filtered or malformed source rows are durably recorded as rejected.
        for snapshot_id in ids:
            if snapshot_id in feature_map:snapshot=feature_map[snapshot_id]
            else:
                raw=await source.fetchrow('SELECT * FROM match_centre_snapshots WHERE id=$1',snapshot_id)
                snapshot={'id':raw['fixture_id'],'captured_at':raw['captured_at'],'state_developer_name':raw['state_developer_name'],
                          'snapshot_minute':raw['snapshot_minute'],'source_raw':dict(raw),'adapter_error':'source_feature_filter'}
            snapshot['home_score_to_increase']=None
            snapshot=json.loads(json.dumps(snapshot,default=serialise,allow_nan=False))
            await repo.submit(p,snapshot,notify=True,key='match_centre:'+str(snapshot_id),source='match_centre_readonly')
    # Advance only to IDs that have been considered. Remaining eligible IDs stay in the next overlap.
    next_cursor=cursor if len(remaining)>batch else max(cursor,rows[-1]['id'])
    async with p.acquire() as c:
        await c.execute("UPDATE v6.collector_cursor SET last_id=$1,updated_at=now() WHERE name='match_centre'",next_cursor)
    return len(ids)

async def run():
    if not enabled('V6_COLLECTOR_ENABLED'):
        print('Collector disabled');return
    dsn=secret('V6_SOURCE_DATABASE_URL')
    if not dsn:raise RuntimeError('Read-only source DSN is required')
    p=await pool();source=None
    if p.get_max_size()<2: raise RuntimeError('Collector requires V6_DB_POOL_SIZE >= 2')
    try:
        # One session holds an advisory lock in the NEW database, preventing duplicate collectors.
        async with p.acquire() as lock:
            if not await lock.fetchval('SELECT pg_try_advisory_lock(610102)'):raise RuntimeError('Another V6 collector is active')
            source=await asyncpg.connect(dsn,command_timeout=3,server_settings={
                'application_name':'sportedge_v6_readonly','default_transaction_read_only':'on',
                'statement_timeout':'1500','lock_timeout':'100','idle_in_transaction_session_timeout':'5000','jit':'off'})
            await init_connection(source)
            while True:
                try:await tick(p,source)
                except Exception as exc:
                    log.error('Source read skipped: %s',type(exc).__name__)
                    if source.is_closed():break # process restart reconnects; cursor survives
                await asyncio.sleep(bounded('V6_SOURCE_POLL_SECONDS',5,2,60))
    finally:
        if source and not source.is_closed():await source.close()
        await p.close()

if __name__=='__main__':asyncio.run(run())
