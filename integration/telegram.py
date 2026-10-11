import asyncio
import html
import logging
import math
import uuid
from urllib.parse import urlsplit
import httpx
from integration.config import enabled, secret, bounded
from integration.database import transaction
from integration.fixture_links import cached_links, valid_event_url

log=logging.getLogger(__name__)
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)

def display(v,limit=120): return html.escape(str(v if v is not None else '-')[:limit])

def format_message(payload):
    s,p=payload['snapshot'],payload['prediction']
    home=display(s.get('home_team_name') or 'Local');away=display(s.get('away_team_name') or 'Visitante')
    lines=['<b>🚨 ALERTA SPORTEDGE</b>','','⚽ <b>Posible próximo gol de '+home+'</b>','',
           f'🏟️ <b>{home}</b>  {display(s.get("home_score"))} - {display(s.get("away_score"))}  <b>{away}</b>',
           f'⏱️ Minuto <b>{display(s.get("snapshot_minute"))}</b>']
    if s.get('league_name'): lines.append('🏆 Liga: <b>'+display(s['league_name'])+'</b>')
    probability=p.get('probability')
    if probability is not None and math.isfinite(float(probability)):
        percentage=float(probability)*100
        if p.get('probability_status')=='uncalibrated_score':
            lines+=['',f'📊 Estimación orientativa del modelo: <b>{percentage:.1f} de 100</b>',
                    'Esta puntuación aún no equivale a una probabilidad real calibrada.']
        else:
            label='Probabilidad ajustada con datos históricos' if p.get('calibration_version') else 'Probabilidad estimada'
            lines+=['',f'📊 {label}: <b>{percentage:.1f}%</b>',
                    f'El modelo estima aproximadamente {percentage:.0f} de cada 100 situaciones similares.']
            if p.get('calibration_validation_status')=='historical_reused_test_prospective_unverified':
                lines.append('Estimación experimental pendiente de validación con partidos nuevos.')
    links=payload.get('fixture_links',{})
    for provider,label in (('365scores','📱 Ver partido en 365Scores'),('betano','🎯 Abrir mercados en Betano')):
        link=links.get(provider,{}) if isinstance(links,dict) else {}
        if isinstance(link,dict) and valid_event_url(provider,link.get('url'),link.get('event_id')):
            lines+=['',f'<a href="{html.escape(link["url"],quote=True)}">{label}</a>']
    lines+=['','🤖 <i>Analytics V6 · experimental</i>']
    if payload.get('source')=='technical_test':
        lines[0]='<b>🧪 PRUEBA TÉCNICA SPORTEDGE V6</b>'
        lines.insert(2,'Ejemplo histórico para comprobar el circuito; no es una alerta en vivo.')
    return '\n'.join(lines)

def request_body(payload,chat):
    text=format_message(payload)
    logos=[]
    for field in ('home_team_logo','away_team_logo'):
        candidate=payload['snapshot'].get(field)
        if not isinstance(candidate,str) or len(candidate)>500:continue
        parsed=urlsplit(candidate)
        host=parsed.hostname or ''
        if parsed.scheme=='https' and (host=='sportmonks.com' or host.endswith('.sportmonks.com')) and not parsed.username and candidate not in logos:logos.append(candidate)
    if len(text)<=1024 and len(logos)==2:
        return 'sendMediaGroup',{'chat_id':chat,'media':[{'type':'photo','media':logos[0],'caption':text,'parse_mode':'HTML'},{'type':'photo','media':logos[1]}]}
    if len(text)<=1024 and len(logos)==1:
        return 'sendPhoto',{'chat_id':chat,'photo':logos[0],'caption':text,'parse_mode':'HTML'}
    return 'sendMessage',{'chat_id':chat,'text':text,'parse_mode':'HTML','link_preview_options':{'is_disabled':True}}

async def claim(p):
    token=uuid.uuid4()
    async with transaction(p) as c:
        # Telegram has no idempotency key: a crashed or timed-out send may already exist remotely.
        await c.execute("UPDATE v6.telegram_outbox SET status='uncertain',last_error='expired_delivery_lease' WHERE status='sending' AND leased_until<now()")
        await c.execute("UPDATE v6.telegram_outbox SET status='suppressed',last_error='stale_notification' WHERE status='pending' AND (created_at<now()-make_interval(secs=>$1) OR (payload->'snapshot'->>'captured_at')::timestamptz<now()-make_interval(secs=>$1))",int(bounded('V6_TELEGRAM_MAX_AGE_SECONDS',300,30,3600)))
        row=await c.fetchrow('''WITH candidate AS (
            SELECT outbox_id FROM v6.telegram_outbox WHERE status='pending' AND attempts<5 AND next_attempt_at<=now()
            ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)
            UPDATE v6.telegram_outbox o SET status='sending',attempts=attempts+1,lease_token=$1,
                leased_until=now()+interval '60 seconds'
            FROM candidate c WHERE o.outbox_id=c.outbox_id RETURNING o.*''',token)
        if row:
            # A small indexed V6-only read. A savepoint keeps cache failures from
            # cancelling the outbox claim. Never contacts either provider here.
            payload=dict(row['payload'])
            links={}
            try:
                async with c.transaction():
                    links=await cached_links(c,payload['snapshot'])
            except Exception as exc:
                log.warning('Fixture links omitted: %s',type(exc).__name__)
            payload['fixture_links']=links
            await c.execute('UPDATE v6.telegram_outbox SET payload=$2 WHERE outbox_id=$1',row['outbox_id'],payload)
            return dict(row) | {'payload':payload}
        return None

async def finish(p,row,status,message_id=None,error=None,delay=5):
    async with transaction(p) as c:
        await c.execute('''UPDATE v6.telegram_outbox SET status=$3,telegram_message_id=$4,last_error=$5,
            next_attempt_at=now()+make_interval(secs=>$6),sent_at=CASE WHEN $3='sent' THEN now() ELSE sent_at END,
            lease_token=NULL,leased_until=NULL WHERE outbox_id=$1 AND lease_token=$2 AND leased_until>now()''',
            row['outbox_id'],row['lease_token'],status,message_id,error,int(delay))

async def deliver_one(p,client):
    row=await claim(p)
    if not row:return False
    # Recheck live enable flag just before the external action.
    if not enabled('V6_TELEGRAM_ENABLED'):
        await finish(p,row,'pending',error='delivery_disabled');return False
    token=secret('V6_TELEGRAM_BOT_TOKEN');chat=secret('V6_TELEGRAM_CHAT_ID')
    if not token or not chat:
        await finish(p,row,'failed',error='telegram_credentials_missing');return True
    try:
        url='https://api.telegram.org' # configurable solely for isolated tests
        import os
        url=os.getenv('V6_TELEGRAM_API_URL',url).rstrip('/')
        method,request=request_body(row['payload'],chat)
        response=await client.post(url+'/bot'+token+'/'+method,json=request,timeout=10)
        body=response.json()
        if not isinstance(body,dict):raise ValueError('Unexpected Telegram response')
        if response.status_code==400 and method!='sendMessage':
            response=await client.post(url+'/bot'+token+'/sendMessage',json={'chat_id':chat,'text':format_message(row['payload']),'parse_mode':'HTML','link_preview_options':{'is_disabled':True}},timeout=10)
            body=response.json()
            if not isinstance(body,dict):raise ValueError('Unexpected Telegram response')
        messages=body.get('result',[])
        if not isinstance(messages,list):messages=[messages]
        ids=[m['message_id'] for m in messages if isinstance(m,dict) and isinstance(m.get('message_id'),int)]
        if response.status_code==429:
            delay=max(1,min(3600,int(body.get('parameters',{}).get('retry_after',30))))
            await finish(p,row,'failed' if row['attempts']>=5 else 'pending',error='telegram_rate_limit',delay=delay)
        elif response.status_code==200 and body.get('ok') is True and ids:
            await finish(p,row,'sent',','.join(map(str,ids)))
        elif 400<=response.status_code<500:
            await finish(p,row,'failed',error='telegram_http_'+str(response.status_code))
        else:
            await finish(p,row,'uncertain',error='telegram_unconfirmed_response')
    except (httpx.ConnectError,httpx.ConnectTimeout):
        await finish(p,row,'failed' if row['attempts']>=5 else 'pending',error='telegram_connection_failed',delay=2**row['attempts'])
    except (httpx.HTTPError,ValueError,KeyError,TypeError):
        await finish(p,row,'uncertain',error='telegram_delivery_unconfirmed')
    return True

async def run():
    from integration.database import pool
    p=await pool()
    try:
        async with httpx.AsyncClient(limits=httpx.Limits(max_connections=1,max_keepalive_connections=1)) as client:
            while True:
                if not enabled('V6_TELEGRAM_ENABLED'): await asyncio.sleep(5);continue
                try:
                    busy=await deliver_one(p,client)
                    await asyncio.sleep(1.1 if busy else 2)
                except Exception as exc:
                    log.error('Telegram loop interrupted: %s',type(exc).__name__)
                    await asyncio.sleep(5)
    finally: await p.close()

if __name__=='__main__': asyncio.run(run())
