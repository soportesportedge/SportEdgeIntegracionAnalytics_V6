import asyncio
import logging
import json
import math
import httpx
from integration.config import ROOT,model_url, secret, bounded
from integration import repository as repo

log=logging.getLogger(__name__)
REGISTRY=json.loads((ROOT/'contracts'/'provenance.json').read_text())['model_registry']

def check_result(result,job):
    if not isinstance(result,dict) or result.get('research_only') is not True:raise ValueError('invalid_model_contract')
    if result.get('family')!=job['family'] or result.get('model_version')!=repo.CONTRACT['version']:raise ValueError('unexpected_model_version')
    if result.get('artifact_sha256')!=REGISTRY['models'][job['family']]['sha256']:raise ValueError('unexpected_model_artifact')
    if set(result.get('engineered_features',{}))!={x['name'] for x in repo.ENGINEERED}:raise ValueError('incomplete_engineering')
    if result.get('target')!='home_score_to_increase':raise ValueError('unexpected_target')
    p=result.get('probability')
    if result.get('eligible') and (p is None or not math.isfinite(float(p)) or not 0<=float(p)<=1):raise ValueError('invalid_probability')
    if not result.get('eligible') and p is not None:raise ValueError('probability_outside_live_horizon')

async def process_one(p, client, job_id=None):
    job=await repo.claim(p,job_id)
    if not job: return False
    try:
        response=await client.post(model_url()+'/v1/predict',
            headers={'Authorization':'Bearer '+secret('V6_MODEL_API_KEY')},
            json={'snapshot':job['input_values'],'family':job['family'],'request_id':str(job['invocation_id'])},
            timeout=bounded('V6_INFERENCE_TIMEOUT',8,1,20))
        if response.status_code!=200:
            await repo.fail(p,job,'model_http_'+str(response.status_code),permanent=response.status_code in (400,401,403,422))
            return True
        result=response.json()
        check_result(result,job)
        # Notifications are experimental and retain the frozen model signal policy.
        payload={'snapshot':job['input_values'],'prediction':result,'source':job['source']} if job['notify'] and result['eligible'] and result['signal'] else None
        await repo.complete(p,job,result,payload)
    except (httpx.HTTPError,ValueError,KeyError,TypeError) as exc:
        await repo.fail(p,job,'inference_'+type(exc).__name__)
    return True

async def run():
    from integration.database import pool
    p=await pool()
    try:
        async with httpx.AsyncClient(limits=httpx.Limits(max_connections=1,max_keepalive_connections=1)) as client:
            while True:
                try:
                    busy=await process_one(p,client)
                    if not busy: await asyncio.sleep(bounded('V6_WORKER_POLL_SECONDS',1,.1,60))
                except Exception as exc:
                    log.error('Inference loop interrupted: %s',type(exc).__name__)
                    await asyncio.sleep(5)
    finally: await p.close()

if __name__=='__main__': asyncio.run(run())
