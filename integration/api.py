import asyncio
from contextlib import asynccontextmanager
import hmac
import json
from typing import Any, Literal
import uuid
import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict
from integration.config import secret, bounded
from integration.database import pool, transaction
from datetime import date, timedelta
from integration.fixture_links import PROVIDERS, day_bounds, validate_events
from integration.link_schedule import configuration as link_configuration
from integration.inference import process_one
from integration import repository as repo

@asynccontextmanager
async def lifespan(app):
    app.state.pool=await pool()
    app.state.http=httpx.AsyncClient(limits=httpx.Limits(max_connections=2,max_keepalive_connections=2))
    app.state.sync_gate=asyncio.Semaphore(1)
    yield
    await app.state.http.aclose()
    await app.state.pool.close()

app=FastAPI(title='SportEdge V6 Integration',version='1.2.0',lifespan=lifespan,
    openapi_tags=[{'name':'Catálogos diarios de partidos',
        'description':'Módulo v6.fixture_catalogues: programación, cobertura y registros locales. La descarga se ejecuta en servicios independientes.'}])

def authorize(authorization):
    expected=secret('V6_INTEGRATION_API_KEY')
    if not expected: raise HTTPException(503,'API authentication is not configured')
    if not authorization or not hmac.compare_digest(authorization,'Bearer '+expected): raise HTTPException(401,'Unauthorized')

class Invocation(BaseModel):
    model_config=ConfigDict(extra='forbid')
    snapshot:dict[str,Any]
    family:str=repo.DEFAULT_FAMILY
    notify:bool=True
    source:str='manual'

@app.get('/health/live')
def live(): return {'status':'ok'}

@app.get('/health/ready')
async def ready():
    async with app.state.pool.acquire() as c:
        exists=await c.fetchval("SELECT to_regclass('v6.match_centre_invocations') IS NOT NULL")
    if not exists: raise HTTPException(503,'Migration required')
    return {'status':'ok','default_family':repo.DEFAULT_FAMILY,'contract_version':repo.CONTRACT['version']}

@app.post('/v1/invocations')
async def submit(req:Invocation, mode:Literal['async','sync']=Query(default='async'),
                 authorization:str|None=Header(default=None),idempotency_key:str|None=Header(default=None)):
    authorize(authorization)
    if len(json.dumps(req.model_dump(),ensure_ascii=False))>256000: raise HTTPException(413,'Snapshot too large')
    job,duplicate=await repo.submit(app.state.pool,req.snapshot,req.family,req.notify,idempotency_key,req.source)
    row=await repo.get(app.state.pool,job)
    if row['status']=='rejected':
        return JSONResponse(status_code=422,content={'invocation_id':str(job),'status':'rejected','reason':row['last_error'],'recorded':True})
    if mode=='sync' and row['status'] not in ('completed','failed'):
        # Timeout does not cancel a durable accepted job. A worker can finish it later.
        async def wait_result():
            async with app.state.sync_gate:
                await process_one(app.state.pool,app.state.http,job)
                while True:
                    state=await repo.get(app.state.pool,job)
                    if state['status'] in ('completed','failed'): return state
                    await asyncio.sleep(.1)
        try: row=await asyncio.wait_for(wait_result(),bounded('V6_SYNC_WAIT_SECONDS',10,1,15))
        except TimeoutError: row=await repo.get(app.state.pool,job)
    code=200 if row['status']=='completed' else 202
    return JSONResponse(status_code=code,headers={'Location':'/v1/invocations/'+str(job)},content=jsonable_encoder({
        'invocation_id':job,'status':row['status'],'duplicate':duplicate,'recorded':True,'result':row['result']}))

@app.get('/v1/invocations/{job}')
async def get(job:uuid.UUID,authorization:str|None=Header(default=None)):
    authorize(authorization)
    return await repo.get(app.state.pool,job)

@app.get('/v1/status')
async def status(authorization:str|None=Header(default=None)):
    authorize(authorization)
    async with app.state.pool.acquire() as c:
        jobs=await c.fetch('SELECT status,count(*) AS count FROM v6.match_centre_invocations GROUP BY status')
        messages=await c.fetch('SELECT status,count(*) AS count FROM v6.telegram_outbox GROUP BY status')
        lag=await c.fetchval("SELECT extract(epoch FROM now()-min(received_at)) FROM v6.match_centre_invocations WHERE status='pending'")
    return {'invocations':[dict(x) for x in jobs],'telegram':[dict(x) for x in messages],'oldest_pending_seconds':lag}

@app.post('/v1/invocations/{job}/retry')
async def retry(job:uuid.UUID,authorization:str|None=Header(default=None)):
    authorize(authorization)
    async with app.state.pool.acquire() as c:
        changed=await c.fetchval("UPDATE v6.match_centre_invocations SET status='pending',attempts=0,next_attempt_at=now(),last_error=NULL WHERE invocation_id=$1 AND status='failed' RETURNING invocation_id",job)
    if not changed: raise HTTPException(409,'Only failed invocations can be retried')
    return {'invocation_id':job,'status':'pending'}


class LinkCatalogue(BaseModel):
    model_config=ConfigDict(extra='forbid')
    match_day:date
    events:list[dict[str,Any]]
    origin:str='authorized_import'


class LinkAlias(BaseModel):
    model_config=ConfigDict(extra='forbid')
    entity:Literal['team','league']
    source_id:int
    provider_name:str


def link_provider(provider):
    if provider not in PROVIDERS:raise HTTPException(404,'Unknown link provider')


@app.put('/v1/fixture-links/catalogues/{provider}',tags=['Catálogos diarios de partidos'])
async def import_catalogue(provider:str,req:LinkCatalogue,authorization:str|None=Header(default=None)):
    authorize(authorization)
    link_provider(provider)
    if len(json.dumps(req.model_dump(mode='json'),ensure_ascii=False))>4*1024*1024:
        raise HTTPException(413,'Catalogue too large')
    try:events=validate_events(req.events,req.match_day,provider=provider)
    except (ValueError,TypeError,OverflowError):raise HTTPException(422,'Invalid catalogue identity, day or event URL') from None
    if not req.origin.strip() or len(req.origin)>200:raise HTTPException(422,'Invalid catalogue origin')
    async with transaction(app.state.pool) as c:
        if not await c.fetchval('SELECT pg_try_advisory_xact_lock(610104)'):
            raise HTTPException(409,'Daily link builder is active; retry import shortly')
        await c.execute('''INSERT INTO v6.fixture_link_catalogues(match_day,provider,events,origin,valid_until)
            VALUES ($1,$2,$3,$4,$5) ON CONFLICT(match_day,provider) DO UPDATE SET events=EXCLUDED.events,
            origin=EXCLUDED.origin,valid_until=EXCLUDED.valid_until,imported_at=now()''',
            req.match_day,provider,events,req.origin,day_bounds(req.match_day)[1]+timedelta(hours=12))
        await c.execute('''INSERT INTO v6.fixture_link_runs(match_day,provider,status,attempts,last_error)
            VALUES ($1,$2,'failed',0,'catalogue_imported_rebuild_pending')
            ON CONFLICT(match_day,provider) DO UPDATE SET next_attempt_at=now(),attempts=0,status='failed',
            last_error='catalogue_imported_rebuild_pending' ''',req.match_day,provider)
    return {'recorded':True,'match_day':req.match_day,'provider':provider,'events':len(events),'build':'pending'}


@app.put('/v1/fixture-links/aliases/{provider}',tags=['Catálogos diarios de partidos'])
async def import_alias(provider:str,req:LinkAlias,authorization:str|None=Header(default=None)):
    authorize(authorization)
    link_provider(provider)
    if req.source_id<=0 or not req.provider_name.strip() or len(req.provider_name)>200:
        raise HTTPException(422,'Invalid alias')
    async with app.state.pool.acquire() as c:
        await c.execute('''INSERT INTO v6.fixture_link_aliases(provider,entity,source_id,provider_name)
            VALUES ($1,$2,$3,$4) ON CONFLICT(provider,entity,source_id) DO UPDATE
            SET provider_name=EXCLUDED.provider_name,updated_at=now()''',provider,req.entity,req.source_id,req.provider_name)
    return {'recorded':True,'provider':provider,'entity':req.entity,'source_id':req.source_id}


@app.get('/v1/fixture-links/status',tags=['Catálogos diarios de partidos'])
async def link_status(match_day:date|None=None,authorization:str|None=Header(default=None)):
    authorize(authorization)
    schedule=link_configuration()
    target=match_day or date.fromisoformat(schedule['local_match_day'])
    async with app.state.pool.acquire() as c:
        runs=await c.fetch('SELECT * FROM v6.fixture_link_runs WHERE match_day=$1 ORDER BY provider',target)
        catalogues=await c.fetch('''SELECT provider,origin,imported_at,valid_until,
            jsonb_array_length(events) AS event_count FROM v6.fixture_link_catalogues
            WHERE match_day=$1 ORDER BY provider''',target)
        counts=await c.fetch('''SELECT provider,status,count(*) AS count FROM v6.fixture_link_registry
            WHERE match_day=$1 GROUP BY provider,status ORDER BY provider,status''',target)
    return {'schedule':schedule,'match_day':target,'runs':[dict(x) for x in runs],
        'catalogues':[dict(x) for x in catalogues],'counts':[dict(x) for x in counts],
        'provider_coverage':{'365scores':'public_daily_calendar','betano':'partial_featured_events'},
        'note':'Schedule configuration and recorded executions; container liveness is inspected with ops.ps1 -Action StatusLinks.'}


@app.get('/v1/fixture-links',tags=['Catálogos diarios de partidos'])
async def get_links(match_day:date,fixture_id:int|None=None,provider:str|None=None,
                    limit:int=Query(default=100,ge=1,le=500),offset:int=Query(default=0,ge=0,le=20000),
                    authorization:str|None=Header(default=None)):
    authorize(authorization)
    if provider:link_provider(provider)
    async with app.state.pool.acquire() as c:
        runs=await c.fetch('SELECT * FROM v6.fixture_link_runs WHERE match_day=$1 ORDER BY provider',match_day)
        rows=await c.fetch('''SELECT * FROM v6.fixture_link_registry WHERE match_day=$1
            AND ($2::bigint IS NULL OR fixture_id=$2) AND ($3::text IS NULL OR provider=$3)
            ORDER BY fixture_id,provider LIMIT $4 OFFSET $5''',match_day,fixture_id,provider,limit,offset)
        counts=await c.fetch('''SELECT provider,status,count(*) AS count FROM v6.fixture_link_registry
            WHERE match_day=$1 GROUP BY provider,status ORDER BY provider,status''',match_day)
    return {'match_day':match_day,'runs':[dict(x) for x in runs],'counts':[dict(x) for x in counts],
            'items':[dict(x) for x in rows],'limit':limit,'offset':offset}


@app.post('/v1/fixture-links/rebuild',tags=['Catálogos diarios de partidos'])
async def rebuild_links(match_day:date,authorization:str|None=Header(default=None)):
    authorize(authorization)
    async with transaction(app.state.pool) as c:
        if not await c.fetchval('SELECT pg_try_advisory_xact_lock(610104)'):
            raise HTTPException(409,'Daily link builder is active')
        for provider in PROVIDERS:
            await c.execute('''INSERT INTO v6.fixture_link_runs(match_day,provider,status,attempts)
                VALUES ($1,$2,'failed',0) ON CONFLICT(match_day,provider) DO UPDATE SET status='failed',
                attempts=0,next_attempt_at=now(),last_error='rebuild_requested' ''',match_day,provider)
    return JSONResponse(status_code=202,content={'match_day':str(match_day),'build':'pending',
        'note':'Processed by the separate links worker on its next tick'})
