import asyncio
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import sys
import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from integration.database import pool
from integration import repository as repo
from integration import telegram
from integration.inference import process_one

def snapshot():
    s=json.loads((ROOT/'tests'/'example_request.json').read_text())['snapshot']
    s['captured_at']=datetime.now(timezone.utc).isoformat()
    return s

def result(job):
    return {'request_id':str(job['invocation_id']),'fixture_id':int(job['id']),'model_version':'v6_precision_20261010',
        'family':job['family'],'artifact_sha256':'test-artifact','target':'home_score_to_increase',
        'probability':.8,'probability_status':'uncalibrated_score','threshold':.4984,'eligible':True,'signal':True,
        'research_only':True,'engineered_features':{r['name']:1.0 for r in repo.ENGINEERED}}

async def setup():
    assert os.environ['V6_DATABASE_URL'].endswith('/sportedge_v6_test')
    p=await pool()
    async with p.acquire() as c:await c.execute('TRUNCATE v6.telegram_outbox,v6.invocation_events,v6.match_centre_invocations,v6.collector_cursor RESTART IDENTITY')
    return p

def run(fn):
    async def wrap():
        p=await setup()
        try:await fn(p)
        finally:await p.close()
    asyncio.run(wrap())

def test_durable_idempotency_and_conflict():
    async def case(p):
        s=snapshot();a,dup=await repo.submit(p,s,key='same')
        b,dup=await repo.submit(p,s,key='same');assert a==b and dup
        row=await repo.get(p,a);assert row['input_values']==s and row['id']==s['id'] and row['status']=='pending'
        with pytest.raises(HTTPException) as exc:await repo.submit(p,{**s,'home_score':5},key='same')
        assert exc.value.status_code==409
    run(case)

def test_invalid_payload_is_recorded():
    async def case(p):
        s=snapshot();s.pop('home_shots_total')
        job,_=await repo.submit(p,s)
        row=await repo.get(p,job);assert row['status']=='rejected' and row['input_values']==s
        assert await repo.claim(p) is None
    run(case)

def test_fractional_fixture_is_rejected_and_original_preserved():
    async def case(p):
        s=snapshot();s['id']=1.5
        job,_=await repo.submit(p,s)
        row=await repo.get(p,job)
        assert row['status']=='rejected' and row['last_error']=='invalid_fixture' and row['input_values']['id']==1.5
    run(case)

def test_atomic_claim_expired_lease_and_fencing():
    async def case(p):
        job,_=await repo.submit(p,snapshot())
        rows=await asyncio.gather(*[repo.claim(p) for _ in range(6)])
        old=next(x for x in rows if x);assert sum(x is not None for x in rows)==1
        async with p.acquire() as c:await c.execute("UPDATE v6.match_centre_invocations SET leased_until=now()-interval '1 second' WHERE invocation_id=$1",job)
        new=await repo.claim(p);assert new['lease_token']!=old['lease_token']
        assert not await repo.complete(p,old,result(old))
        assert await repo.complete(p,new,result(new))
        row=await repo.get(p,job)
        assert row['status']=='completed' and all(row[x['name']]==1 for x in repo.ENGINEERED)
    run(case)

def test_outbox_is_atomic_and_deduplicates_score_episode():
    async def case(p):
        s=snapshot()
        for n in range(2):
            job,_=await repo.submit(p,s,key='k'+str(n));j=await repo.claim(p,job);r=result(j)
            await repo.complete(p,j,r,{'snapshot':s,'prediction':r})
        async with p.acquire() as c:
            assert await c.fetchval('SELECT count(*) FROM v6.telegram_outbox')==1
            assert await c.fetchval("SELECT count(*) FROM v6.match_centre_invocations WHERE status='completed'")==2
    run(case)

def test_inference_unavailable_preserves_durable_retry():
    async def case(p):
        job,_=await repo.submit(p,snapshot())
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(503))) as client:await process_one(p,client)
        row=await repo.get(p,job)
        assert row['status']=='pending' and row['attempts']==1 and row['last_error']=='model_http_503'
    run(case)

@pytest.mark.parametrize('mode',['success','rate_limit','timeout','bad_request'])
def test_telegram_delivery_outcomes(monkeypatch,mode):
    monkeypatch.setenv('V6_TELEGRAM_ENABLED','true');monkeypatch.setenv('V6_TELEGRAM_BOT_TOKEN','test-token');monkeypatch.setenv('V6_TELEGRAM_CHAT_ID','test-chat')
    async def case(p):
        s=snapshot();job,_=await repo.submit(p,s);j=await repo.claim(p,job);r=result(j)
        await repo.complete(p,j,r,{'snapshot':s,'prediction':r})
        sent=[]
        def handler(request):
            sent.append(json.loads(request.content));assert sent[0]['chat_id']=='test-chat'
            if mode=='timeout':raise httpx.ReadTimeout('simulated',request=request)
            if mode=='rate_limit':return httpx.Response(429,json={'ok':False,'parameters':{'retry_after':60}})
            if mode=='bad_request':return httpx.Response(400,json={'ok':False})
            return httpx.Response(200,json={'ok':True,'result':{'message_id':123}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert await telegram.deliver_one(p,client)
            assert not await telegram.deliver_one(p,client)
        async with p.acquire() as c:row=await c.fetchrow('SELECT * FROM v6.telegram_outbox')
        assert row['status']=={'success':'sent','rate_limit':'pending','timeout':'uncertain','bad_request':'failed'}[mode] and len(sent)==1
        if mode=='success':assert row['telegram_message_id']=='123'
    run(case)

def test_telegram_format_and_stale_suppression():
    s=snapshot();s.update(home_team_name='<Local>',league_name='Liga & prueba')
    text=telegram.format_message({'snapshot':s,'prediction':{'probability':.75,'threshold':.5,'probability_status':'uncalibrated_score'}})
    assert 'umbral' not in text.lower() and '75.0 de 100' in text and 'Liga &amp; prueba' in text and '&lt;Local&gt;' in text
    async def case(p):
        s['captured_at']='2020-01-01T00:00:00+00:00'
        job,_=await repo.submit(p,s);j=await repo.claim(p,job);r=result(j)
        await repo.complete(p,j,r,{'snapshot':s,'prediction':r})
        assert await telegram.claim(p) is None
        async with p.acquire() as c:assert await c.fetchval('SELECT status FROM v6.telegram_outbox')=='suppressed'
    run(case)

@pytest.mark.parametrize('mode',['album','photo','fallback','malformed'])
def test_telegram_logos_fallback_and_unconfirmed_response(monkeypatch,mode):
    monkeypatch.setenv('V6_TELEGRAM_ENABLED','true');monkeypatch.setenv('V6_TELEGRAM_BOT_TOKEN','test-token');monkeypatch.setenv('V6_TELEGRAM_CHAT_ID','test-chat')
    async def case(p):
        s=snapshot();s['home_team_logo']='https://cdn.sportmonks.com/images/soccer/teams/1/1.png'
        if mode!='photo':s['away_team_logo']='https://cdn.sportmonks.com/images/soccer/teams/2/2.png'
        job,_=await repo.submit(p,s);j=await repo.claim(p,job);r=result(j)
        await repo.complete(p,j,r,{'snapshot':s,'prediction':r})
        calls=[]
        def handler(request):
            method=request.url.path.rsplit('/',1)[-1];calls.append(method)
            body=json.loads(request.content);assert body['chat_id']=='test-chat'
            if mode=='malformed':return httpx.Response(200,json=[])
            if mode=='fallback' and len(calls)==1:return httpx.Response(400,json={'ok':False})
            messages=[{'message_id':201},{'message_id':202}] if method=='sendMediaGroup' else {'message_id':203}
            return httpx.Response(200,json={'ok':True,'result':messages})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:assert await telegram.deliver_one(p,client)
        async with p.acquire() as c:row=await c.fetchrow('SELECT * FROM v6.telegram_outbox')
        assert row['status']==('uncertain' if mode=='malformed' else 'sent')
        assert calls==(['sendMediaGroup','sendMessage'] if mode=='fallback' else ['sendPhoto'] if mode=='photo' else ['sendMediaGroup'])
        if mode=='album':assert row['telegram_message_id']=='201,202'
    run(case)

def test_telegram_ignores_untrusted_logo_urls():
    s=snapshot();s.update(home_team_logo='https://sportmonks.com.attacker.invalid/logo.png',away_team_logo='http://cdn.sportmonks.com/logo.png')
    method,body=telegram.request_body({'snapshot':s,'prediction':{'probability':.7}},'test-chat')
    assert method=='sendMessage' and 'text' in body

def test_http_api_registers_rejection_and_async_acceptance(monkeypatch):
    run(lambda p:asyncio.sleep(0))
    monkeypatch.setenv('V6_INTEGRATION_API_KEY','test-integration-key')
    from integration.api import app
    headers={'Authorization':'Bearer test-integration-key','Idempotency-Key':'http-key'}
    with TestClient(app) as client:
        s=snapshot()
        assert client.post('/v1/invocations',json={'snapshot':s}).status_code==401
        r=client.post('/v1/invocations',json={'snapshot':s},headers=headers);assert r.status_code==202
        job=r.json()['invocation_id']
        assert client.get('/v1/invocations/'+job,headers=headers).json()['input_values']==s
        assert client.post('/v1/invocations',json={'snapshot':s},headers=headers).json()['duplicate']
        s.pop('home_shots_total');headers['Idempotency-Key']='invalid'
        r=client.post('/v1/invocations',json={'snapshot':s},headers=headers)
        assert r.status_code==422 and r.json()['recorded']
