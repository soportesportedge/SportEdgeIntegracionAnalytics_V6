from datetime import datetime, timezone
import hashlib
import json
import math
import uuid
from fastapi import HTTPException
from integration.config import ROOT, bounded
from integration.database import transaction

CONTRACT = json.loads((ROOT/'contracts'/'input_contract.json').read_text())
SOURCE = CONTRACT['source_columns']
ENGINEERED = CONTRACT['engineering_columns']
FAMILIES = CONTRACT['families']
CONTEXT = [{'name':n,'sql_type':'text'} for n in ('league_name','home_team_logo','away_team_logo')]+[{'name':'source_raw','sql_type':'jsonb'}]

def canonical(value): return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)

def validation_error(snapshot, family):
    if family not in FAMILIES: return 'unknown_family'
    missing = [c for c in CONTRACT['predictors']+['id','captured_at','state_developer_name'] if c not in snapshot]
    if missing: return 'missing_fields:' + ','.join(missing)
    if any(snapshot.get(c) is not None for c in CONTRACT['target_columns']): return 'future_target_present'
    try:
        fixture=int(snapshot['id'])
        if fixture < 1 or isinstance(snapshot['id'],bool) or float(snapshot['id']) != fixture: return 'invalid_fixture'
        stamp=datetime.fromisoformat(str(snapshot['captured_at']).replace('Z','+00:00'))
        if stamp.tzinfo is None: return 'timestamp_requires_timezone'
        minute=float(snapshot['snapshot_minute'])
        if isinstance(snapshot['snapshot_minute'],bool) or not math.isfinite(minute) or not 0 <= minute <= 200: return 'invalid_minute'
        for c in ['home_score','away_score']:
            v=float(snapshot[c])
            if isinstance(snapshot[c],bool) or not math.isfinite(v) or v < 0 or not v.is_integer(): return 'invalid_score'
        for c in CONTRACT['predictors']:
            v=snapshot[c]
            if v is None or c in ('league_id','cluster'): continue
            if isinstance(v,str) and v.lower() in ('t','f','true','false'): continue
            if not math.isfinite(float(v)): return 'nonfinite_predictor:' + c
        canonical(snapshot)
    except (ValueError, TypeError, OverflowError): return 'invalid_snapshot'
    return None

def projection(values, columns):
    """Typed searchable copy; the original submitted JSON is retained separately."""
    result={}
    for spec in columns:
        name,typ=spec['name'],spec['sql_type'];v=values.get(name)
        if v is None: result[name]=None;continue
        try:
            if typ=='boolean':
                if isinstance(v,str) and v.lower() not in ('t','f','true','false','1','0'): raise ValueError()
                result[name]=str(v).lower() in ('t','true','1') if isinstance(v,str) else bool(v)
            elif typ in ('integer','bigint'): result[name]=int(v)
            elif typ=='double precision':
                v={'t':1,'true':1,'f':0,'false':0}.get(v.lower(),v) if isinstance(v,str) else v
                result[name]=float(v) if math.isfinite(float(v)) else None
            elif typ=='timestamp with time zone':
                stamp=datetime.fromisoformat(str(v).replace('Z','+00:00'))
                result[name]=stamp.isoformat() if stamp.tzinfo else None
            elif typ=='jsonb':result[name]=v
            else: result[name]=str(v)
        except (ValueError,TypeError,OverflowError): result[name]=None
    return result

async def hydrate(conn, invocation_id, values, columns):
    data=projection(values,columns)
    names=[q['name'] for q in columns]
    assignment=', '.join('"'+n+'"=typed."'+n+'"' for n in names)
    await conn.execute(f'UPDATE v6.match_centre_invocations j SET {assignment} FROM jsonb_populate_record(NULL::v6.match_centre_invocations,$1::jsonb) AS typed WHERE j.invocation_id=$2',data,invocation_id)

async def submit(p, snapshot, family='random_forest', notify=True, key=None, source='manual'):
    error=validation_error(snapshot,family)
    body={'snapshot':snapshot,'family':family,'notify':notify,'source':source}
    try: digest=hashlib.sha256(canonical(body).encode()).hexdigest()
    except ValueError: raise HTTPException(422,'JSON must contain finite values')
    idem=key or digest
    if len(idem)>200: raise HTTPException(422,'Idempotency key is too long')
    job=uuid.uuid4()
    async with transaction(p) as c:
        await c.execute('SELECT pg_advisory_xact_lock(610101)')
        prior=await c.fetchrow('SELECT invocation_id,input_sha256 FROM v6.match_centre_invocations WHERE idempotency_key=$1',idem)
        if prior:
            if prior['input_sha256']!=digest: raise HTTPException(409,'Idempotency key conflicts with different input')
            return prior['invocation_id'], True
        size=await c.fetchval("SELECT count(*) FROM v6.match_centre_invocations WHERE status IN ('pending','processing')")
        if size >= int(bounded('V6_MAX_BACKLOG',5000,1,100000)): raise HTTPException(503,'Durable queue capacity exhausted')
        await c.execute('''INSERT INTO v6.match_centre_invocations
            (invocation_id,idempotency_key,input_sha256,input_values,family,notify,source,status,last_error)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)''',job,idem,digest,snapshot,family,notify,source,'rejected' if error else 'pending',error)
        await hydrate(c,job,snapshot,SOURCE)
        await hydrate(c,job,snapshot,CONTEXT)
        await c.execute('INSERT INTO v6.invocation_events(invocation_id,event,detail) VALUES ($1,$2,$3)',job,'rejected' if error else 'accepted',{'reason':error})
    return job,False

async def get(p, job):
    async with p.acquire() as c:
        row=await c.fetchrow('SELECT * FROM v6.match_centre_invocations WHERE invocation_id=$1',job)
    if not row: raise HTTPException(404,'Unknown invocation')
    return dict(row)

async def claim(p, job=None):
    token=uuid.uuid4()
    async with transaction(p) as c:
        await c.execute("""UPDATE v6.match_centre_invocations SET status='failed',last_error='lease_attempts_exhausted'
            WHERE status='processing' AND leased_until<now() AND attempts>=5""")
        row=await c.fetchrow('''WITH candidate AS (
            SELECT invocation_id FROM v6.match_centre_invocations
            WHERE ($1::uuid IS NULL OR invocation_id=$1) AND attempts<5 AND next_attempt_at<=now()
            AND (status='pending' OR (status='processing' AND leased_until<now()))
            ORDER BY received_at FOR UPDATE SKIP LOCKED LIMIT 1)
            UPDATE v6.match_centre_invocations j SET status='processing',lease_token=$2,
                leased_until=now()+interval '60 seconds',attempts=attempts+1,updated_at=now()
            FROM candidate c WHERE j.invocation_id=c.invocation_id RETURNING j.*''',job,token)
        if row:
            await c.execute('INSERT INTO v6.invocation_events(invocation_id,event,detail) VALUES ($1,$2,$3)',row['invocation_id'],'processing',{'attempt':row['attempts']})
        return dict(row) if row else None

async def fail(p, job, error, permanent=False):
    async with transaction(p) as c:
        row=await c.fetchrow('''UPDATE v6.match_centre_invocations SET status=$3,last_error=$4,
            next_attempt_at=now()+make_interval(secs=>$5),leased_until=NULL,lease_token=NULL,updated_at=now()
            WHERE invocation_id=$1 AND lease_token=$2 AND leased_until>now() RETURNING invocation_id''',
            job['invocation_id'],job['lease_token'],'failed' if permanent or job['attempts']>=5 else 'pending',
            error[:500],min(300,2**job['attempts']))
        if row: await c.execute('INSERT INTO v6.invocation_events(invocation_id,event,detail) VALUES ($1,$2,$3)',job['invocation_id'],'inference_failed',{'error':error[:500]})

async def complete(p, job, result, telegram_payload=None):
    if result.get('request_id') != str(job['invocation_id']) or result.get('fixture_id') != int(job['id']):
        raise ValueError('inference_identity_mismatch')
    async with transaction(p) as c:
        row=await c.fetchrow('''UPDATE v6.match_centre_invocations SET status='completed',result=$3,
            model_version=$4,probability=$5,threshold=$6,eligible=$7,signal=$8,
            engineered_values=$9,completed_at=now(),updated_at=now(),leased_until=NULL,lease_token=NULL,last_error=NULL
            WHERE invocation_id=$1 AND lease_token=$2 AND leased_until>now() RETURNING invocation_id''',
            job['invocation_id'],job['lease_token'],result,result['model_version'],result.get('probability'),
            result['threshold'],result['eligible'],result['signal'],result['engineered_features'])
        if not row: return False
        await hydrate(c,job['invocation_id'],result['engineered_features'],ENGINEERED)
        await c.execute('INSERT INTO v6.invocation_events(invocation_id,event,detail) VALUES ($1,$2,$3)',job['invocation_id'],'completed',{'artifact_sha256':result['artifact_sha256']})
        if telegram_payload:
            s=job['input_values']
            policy=result.get('decision_policy_version',result['model_version'])
            notification_key=':'.join(str(v) for v in [s['id'],result['target'],policy,s['home_score'],s['away_score']])
            await c.execute('''INSERT INTO v6.telegram_outbox(outbox_id,invocation_id,notification_key,payload)
                VALUES ($1,$2,$3,$4) ON CONFLICT(notification_key) DO NOTHING''',uuid.uuid4(),job['invocation_id'],notification_key,telegram_payload)
    return True
