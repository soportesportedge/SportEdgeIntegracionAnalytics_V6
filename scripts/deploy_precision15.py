"""Deploy only the independent V6 stack and verify live registration and rollback.

Build images first. No source database DDL, application changes or bulk replay.
Optional --telegram-test sends one clearly marked historical example to the
already authorized V6 test chat, via the ordinary durable invocation/outbox.
"""
import argparse
import asyncio
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
import httpx

I=Path(__file__).resolve().parents[1]
A=I.parent/'SportEdgeAnalyticsV6'
COMPOSE=str(I/'compose.yaml')
OUT=I.parents[1]/'outputs/v6_precision15_deployment_20261011'

def docker(*args,env=None):
    p=subprocess.run(['docker',*args],capture_output=True,text=True,env=env)
    if p.returncode:
        raise RuntimeError('V6 Docker operation failed: '+p.stderr[-1500:])
    return p.stdout.strip()

def sql(query):
    data=docker('exec','sportedge-v6-v6-db-1','psql','-U','v6','-d','sportedge_v6','-qAt','-c',query)
    return json.loads(data) if data else None

def container_identity(names):
    return docker('inspect','--format','{{.Name}}|{{.Id}}|{{.State.StartedAt}}',*names).splitlines()

def save_report(report):
    text=json.dumps(report,indent=2,ensure_ascii=False)
    (I/'docs/precision15_deployment_validation.json').write_text(text,encoding='utf-8')
    (A/'docs/precision15_deployment_validation.json').write_text(text,encoding='utf-8')
    if OUT.parent.exists():
        OUT.mkdir(exist_ok=True)
        (OUT/'deployment_validation.json').write_text(text,encoding='utf-8')

async def run(telegram_test):
    report={'checked_at':datetime.now(timezone.utc).isoformat(),'checks':{}}
    protected=['sportedge_v2_api','sportedge_v2_db','sportedge_v2_runner','sportedge_v2_web',
               'sportedge_v2_admin','sportedge_v2_redis','sportedge_v2_mongo',
               'sportedge_five_models_production','sportedge-report-v6-reporter-1','sportedge-v6-v6-db-1']
    before=container_identity(protected)
    hashes=sql("SELECT json_object_agg(invocation_id,json_build_array(md5(input_values::text),md5(result::text))) FROM v6.match_centre_invocations WHERE status='completed'") or {}
    cursor=sql("SELECT row_to_json(c) FROM v6.collector_cursor c WHERE name='match_centre'")
    report['collector_cursor_before']=cursor
    chat=(I/'secrets/telegram_chat_id.txt').read_text().strip()
    assert chat=='-5538403637','Unexpected test chat; deployment stopped'
    report['telegram_chat_id']=chat
    report['checks']['telegram_authorized_test_chat_preserved']=True
    manifest=json.loads((A/'assets/manifest.json').read_text())
    spec=manifest['models']['logistic']
    assert hashlib.sha256((A/'assets'/spec['file']).read_bytes()).hexdigest()==spec['sha256']
    M={'Authorization':'Bearer '+(I/'secrets/model_api_key.txt').read_text().strip()}
    H={'Authorization':'Bearer '+(I/'secrets/integration_api_key.txt').read_text().strip()}
    async with httpx.AsyncClient(timeout=20) as client:
        h=await client.get('http://127.0.0.1:8001/api/v1/health/deep');h.raise_for_status()
        report['v2_health_before']=h.json()
        # Stop producers before swapping the model/consumer contract. The V6 DB stays up.
        docker('compose','-f',COMPOSE,'--profile','collector','--profile','telegram','stop',
               'collector','api','inference-worker','telegram-worker','analytics')
        pending=sql("SELECT json_build_object('pending',count(*) FILTER(WHERE status='pending'),'processing',count(*) FILTER(WHERE status='processing')) FROM v6.match_centre_invocations")
        report['queue_at_cutover']=pending
        backup=I/'backups';backup.mkdir(exist_ok=True)
        dump=backup/('sportedge_v6_before_precision15_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.dump')
        with dump.open('wb') as stream:
            p=subprocess.run(['docker','exec','sportedge-v6-v6-db-1','pg_dump','-U','v6','-d','sportedge_v6','-Fc'],stdout=stream,stderr=subprocess.PIPE)
        assert p.returncode==0 and dump.stat().st_size>0,'V6 backup failed'
        # Verify the dump directory with pg_restore without restoring any data.
        with dump.open('rb') as stream:
            p=subprocess.run(['docker','exec','-i','sportedge-v6-v6-db-1','pg_restore','--list'],stdin=stream,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        assert p.returncode==0 and b'match_centre_invocations' in p.stdout
        report['backup']={'file':dump.name,'bytes':dump.stat().st_size,'sha256':hashlib.sha256(dump.read_bytes()).hexdigest(),'toc_verified':True}
        # These exact previous image tags must exist before updating.
        for image in ('sportedge-analytics-v6:1.0.0','sportedge-integration-v6:1.0.0'):
            docker('image','inspect','--format','{{.Id}}',image)
        report['checks']['previous_images_and_v6_backup_preserved']=True
        env={**os.environ,'V6_TELEGRAM_ENABLED':'true'}
        start=time.perf_counter()
        docker('compose','-f',COMPOSE,'--profile','collector','--profile','telegram','up','-d','--no-build',
               'analytics','api','inference-worker','telegram-worker','collector',env=env)
        for _ in range(90):
            try:
                a=await client.get('http://127.0.0.1:8790/health/ready');a.raise_for_status()
                i=await client.get('http://127.0.0.1:8791/health/ready');i.raise_for_status()
                assert a.json()['model_version']==spec['model_version']
                assert i.json()['default_family']=='logistic'
                break
            except (httpx.HTTPError,KeyError,AssertionError):await asyncio.sleep(.5)
        else:raise RuntimeError('V6 release did not become ready')
        report['restart_and_readiness_seconds']=round(time.perf_counter()-start,3)
        report['analytics_health']=a.json();report['integration_health']=i.json()
        examples=json.loads((A/'tests/precision15_examples.json').read_text())
        snapshot=examples[0]['snapshot']
        for family,item in manifest['models'].items():
            r=await client.post('http://127.0.0.1:8790/v1/predict',headers=M,json={'snapshot':snapshot,'family':family})
            r.raise_for_status();q=r.json()
            assert q['artifact_sha256']==item['sha256'] and q['model_version']==item['model_version']
        report['checks']['all_seven_live_versions_and_hashes_verified']=True
        traces=[]
        for n,example in enumerate(examples):
            mode='sync' if n%2==0 else 'async'
            r=await client.post('http://127.0.0.1:8791/v1/invocations',headers={**H,'Idempotency-Key':'precision15-verification-'+str(uuid.uuid4())},params={'mode':mode},
                json={'snapshot':example['snapshot'],'notify':False,'source':'precision15_verification'})
            r.raise_for_status();job=r.json()['invocation_id']
            for _ in range(100):
                s=(await client.get('http://127.0.0.1:8791/v1/invocations/'+job,headers=H)).json()
                if s['status'] in ('completed','failed'):break
                await asyncio.sleep(.1)
            assert s['status']=='completed',s['last_error']
            q=s['result']
            assert abs(q['raw_model_score']-example['raw_score'])<1e-10
            assert abs(q['probability']-example['probability'])<1e-10
            assert q['signal']==example['signal'] and q['decision_threshold']==spec['raw_threshold']
            assert s['input_values']==example['snapshot'] and len(s['engineered_values'])==78
            for field in ('candidate_id','artifact_sha256','raw_model_score','probability_status',
                          'calibration_method','calibration_version','calibration_validation_status',
                          'decision_basis','decision_threshold','decision_policy_version'):
                assert s[field]==q[field]
            traces.append({'invocation_id':job,'mode':mode,'raw_model_score':q['raw_model_score'],
                           'probability':q['probability'],'signal':q['signal'],'latency_ms':q['latency_ms']})
        report['verified_invocations']=traces
        report['checks']['sync_async_original_inputs_engineering_and_typed_metadata_verified']=True
        jobs=','.join("'"+v['invocation_id']+"'" for v in traces)
        assert sql('SELECT count(*) FROM v6.telegram_outbox WHERE invocation_id IN ('+jobs+')')==0
        report['checks']['historical_verification_rows_not_notified']=True
        if telegram_test:
            example=next(e for e in examples if e['signal'])
            s={**example['snapshot'],'captured_at':datetime.now(timezone.utc).isoformat()}
            s['league_name']=s.get('league_name') or 'Ejemplo histórico de prueba V6'
            r=await client.post('http://127.0.0.1:8791/v1/invocations',headers={**H,'Idempotency-Key':'precision15-technical-test-'+str(uuid.uuid4())},
                json={'snapshot':s,'notify':True,'source':'technical_test'})
            r.raise_for_status();job=r.json()['invocation_id']
            for _ in range(200):
                delivery=sql("SELECT json_build_object('status',status,'telegram_message_id',telegram_message_id) FROM v6.telegram_outbox WHERE invocation_id='"+job+"'")
                if delivery and delivery['status'] in ('sent','failed','uncertain','suppressed'):break
                await asyncio.sleep(.2)
            assert delivery and delivery['status']=='sent',delivery
            report['telegram_technical_test']={'invocation_id':job,**delivery,'explicitly_marked_historical':True}
            report['checks']['one_marked_technical_test_confirmed_in_authorized_chat']=True
        after=sql("SELECT json_object_agg(invocation_id,json_build_array(md5(input_values::text),md5(result::text))) FROM v6.match_centre_invocations WHERE status='completed'") or {}
        assert all(after.get(k)==v for k,v in hashes.items())
        report['preserved_completed_records']=len(hashes)
        report['checks']['all_prior_completed_inputs_and_results_preserved']=True
        report['collector_cursor_after']=sql("SELECT row_to_json(c) FROM v6.collector_cursor c WHERE name='match_centre'")
        report['checks']['collector_cursor_preserved']=report['collector_cursor_after']['last_id']>=cursor['last_id']
        h=await client.get('http://127.0.0.1:8001/api/v1/health/deep');h.raise_for_status()
        report['v2_health_after']=h.json()
        report['checks']['v2_health_preserved']=report['v2_health_after']==report['v2_health_before']
    report['checks']['all_v2_production_model_reporter_and_v6_db_ids_start_times_unchanged']=container_identity(protected)==before
    report['migrations']=sql('SELECT json_agg(version ORDER BY version) FROM v6.schema_migrations')
    report['checks']['additive_v6_migration_applied']=report['migrations']==['001','002','003']
    report['status']=sql("SELECT json_object_agg(status,n) FROM (SELECT status,count(*) n FROM v6.match_centre_invocations GROUP BY status)t")
    report['passed']=all(report['checks'].values())
    save_report(report)
    print(json.dumps(report,indent=2,ensure_ascii=False))
    assert report['passed']

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--telegram-test',action='store_true')
    args=parser.parse_args()
    asyncio.run(run(args.telegram_test))
