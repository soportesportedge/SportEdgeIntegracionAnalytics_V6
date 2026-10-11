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
from integration.database import pool
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

app=FastAPI(title='SportEdge V6 Integration',version='1.1.0',lifespan=lifespan)

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
