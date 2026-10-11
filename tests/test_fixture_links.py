import asyncio
from datetime import date,datetime,timedelta,timezone
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo
import httpx
import pytest
from fastapi.testclient import TestClient

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from integration import fixture_links as links,link_worker,telegram
from integration.link_catalogue import download,scores365_events,CatalogueError
from integration.betano_web import calendar_events,top_events,publish,ORIGIN
from integration.link_schedule import configuration as link_configuration
from integration.database import pool

DAY=date(2026,10,11)


def event(provider='365scores',event_id='7001',starting_at='2026-10-11T18:00:00Z',**changes):
    url=('https://www.365scores.com/es/football/match/liga-1/local-visitante-1-2#id='+event_id
         if provider=='365scores' else 'https://www.betano.pe/odds/local-visitante/'+event_id+'/')
    return {'event_id':event_id,'starting_at':starting_at,'home':'Atlético Local','away':'Visitante',
            'league':'Liga 1','country':'Peru','url':url} | changes


def fixture(starting_at='2026-10-11T18:00:00Z',**changes):
    return {'fixture_id':1001,'starting_at':links.instant(starting_at),'home_team_id':10,
            'away_team_id':20,'league_id':30,'home':'Atletico Local','away':'Visitante','league':'Liga 1','country':'Perú'} | changes


def test_lima_day_and_2am_schedule(monkeypatch):
    monkeypatch.delenv('V6_LINK_BUILD_HOUR',raising=False)
    monkeypatch.delenv('V6_LINK_BUILD_MINUTE',raising=False)
    assert links.day_bounds(DAY)==(links.instant('2026-10-11T05:00:00Z'),links.instant('2026-10-12T05:00:00Z'))
    assert link_worker.due_day(links.instant('2026-10-11T06:59:59Z')) is None
    assert link_worker.due_day(links.instant('2026-10-11T07:00:00Z'))==DAY
    assert link_worker.due_day(links.instant('2026-10-12T04:59:00Z'))==DAY
    assert link_worker.due_day(links.instant('2026-10-12T05:00:00Z')) is None
    assert link_worker.due_day(links.instant('2026-10-12T07:00:00Z'))==DAY+timedelta(days=1)
    schedule=link_configuration(links.instant('2026-10-11T06:59:59Z'))
    assert schedule['daily_start']=='02:00' and schedule['timezone']=='America/Lima'
    assert schedule['next_daily_start_at']=='2026-10-11T02:00:00-05:00'
    assert link_configuration(links.instant('2026-10-11T07:00:00Z'))['next_daily_start_at']=='2026-10-12T02:00:00-05:00'
    monkeypatch.setenv('V6_LINK_BUILD_HOUR','3')
    monkeypatch.setenv('V6_LINK_BUILD_MINUTE','15')
    assert link_worker.due_day(links.instant('2026-10-11T08:14:59Z')) is None
    assert link_worker.due_day(links.instant('2026-10-11T08:15:00Z'))==DAY


def test_unique_match_requires_teams_league_country_kickoff():
    index=links.event_index([event()])
    assert links.resolve(fixture(),index)['status']=='matched'
    for change in ({'home':'Visitante','away':'Atlético Local'},{'home':'Atletico Local U21'},
                   {'league':'Liga 2'},{'country':'Chile'},{'starting_at':links.instant('2026-10-11T19:00Z')}):
        assert links.resolve(fixture(**change),index)['status']=='not_found'
    assert links.resolve(fixture(home_team_id=None),index)['status']=='not_found'


def test_aliases_and_ambiguous_matches_are_explicit():
    e=event(home='Local FC')
    assert links.resolve(fixture(),links.event_index([e]))['status']=='not_found'
    assert links.resolve(fixture(),links.event_index([e]),{('team',10):'Local FC'})['status']=='matched'
    assert links.resolve(fixture(),links.event_index([event(),event(event_id='7002')]))['status']=='ambiguous'


@pytest.mark.parametrize('provider,url',[
    ('365scores','https://www.365scores.com/es'),('betano','https://www.betano.pe'),
    ('365scores','https://www.365scores.com/es/football/match/liga-1/local-visitante-1-2'),
    ('365scores','https://www.365scores.com/es/football/match/liga-1/local-visitante-1-2#id=9999'),
    ('betano','https://www.betano.pe/odds/local-visitante/9999/'),
    ('betano','https://betano.pe.attacker.invalid/odds/local-visitante/7001/'),
    ('betano','https://user@www.betano.pe/odds/local-visitante/7001/'),
    ('betano','https://www.betano.pe:123/odds/local-visitante/7001/'),
    ('betano','http://www.betano.pe/odds/local-visitante/7001/'),
    ('betano','https://www.betano.pe/odds/local-visitante/7001/\n'),
])
def test_unsafe_or_non_specific_urls_are_rejected(provider,url):
    assert not links.valid_event_url(provider,url,'7001')


def test_betano_spanish_canonical_event_route():
    url='https://www.betano.pe/cuotas-de-partido/utc-de-cajamarca-sporting-cristal/93284390/'
    assert links.valid_event_url('betano',url,'93284390')
    assert not links.valid_event_url('betano',url,'7001')


def test_public_betano_dom_calendar_identity_timezone_and_tracking():
    row={'event_id':'93284390','href':'/cuotas-de-partido/utc-de-cajamarca-sporting-cristal/93284390/?_gl=tracking',
         'teams':['UTC De Cajamarca','Sporting Cristal'],'competition':'Perú - Liga 1','clock':['11/10','13:00']}
    events=calendar_events([row,row],DAY)
    assert len(events)==1
    assert events[0]['home']=='UTC De Cajamarca'
    assert events[0]['starting_at']=='2026-10-11T18:00:00+00:00'
    assert events[0]['country']=='Peru' and events[0]['league']=='Liga 1'
    assert events[0]['url']=='https://www.betano.pe/cuotas-de-partido/utc-de-cajamarca-sporting-cristal/93284390/'
    for changes in ({'event_id':'9999'},{'teams':['Local']},{'clock':['12/10','13:00']},
                    {'href':'https://attacker.invalid/odds/partido/93284390/'},
                    {'competition':'Sin liga'},{'clock':['11/10','EN VIVO']}):
        with pytest.raises(ValueError):calendar_events([row|changes],DAY)
    with pytest.raises(ValueError):calendar_events([row,row|{'teams':['Otro','Sporting Cristal']}],DAY)
    with pytest.raises(ValueError):calendar_events([],DAY)


def test_native_betano_featured_contract_filters_day_and_never_uses_selections():
    body={'data':{'topEventsV2':{'events':{'93284390':{'id':93284390,'sportId':'FOOT',
        'startTime':1791741600000,'leagueId':17079,'zoneId':11380,
        'participants':[{'name':'UTC De Cajamarca'},{'name':'Sporting Cristal'}],
        'url':'/cuotas-de-partido/utc-de-cajamarca-sporting-cristal/93284390/'}},
        'leagues':{'17079':{'name':'Liga 1'}},'zones':{'11380':{'name':'Perú'}},
        'selections':{'unrelated':{'price':999}},'markets':{'unrelated':{}}}}}
    events=top_events(body,DAY)
    assert len(events)==1 and events[0]['event_id']=='93284390'
    assert events[0]['home']=='UTC De Cajamarca' and events[0]['league']=='Liga 1'
    assert events[0]['starting_at']=='2026-10-11T18:00:00+00:00'
    for changes in ({'id':1},{'participants':[{'name':'UTC'}]},
                    {'url':'https://attacker.invalid/odds/partido/93284390/'},
                    {'startTime':1791655200000}):
        invalid=json.loads(json.dumps(body))
        invalid['data']['topEventsV2']['events']['93284390'].update(changes)
        with pytest.raises(ValueError):top_events(invalid,DAY)


def test_catalogue_validation_is_atomic_and_strict():
    assert len(links.validate_events([event()],DAY,provider='365scores'))==1
    for events in ([event(),event()], [event(starting_at='2026-10-11T18:00:00')],
                   [event(starting_at='2026-10-11T04:59:59Z')],[event(league='')],[event(event_id='')]):
        with pytest.raises(ValueError):links.validate_events(events,DAY,provider='365scores')


def test_bulk_download_once_and_provider_failures():
    async def case():
        calls=[]
        def handler(req):
            calls.append(req)
            return httpx.Response(200,json={'match_day':str(DAY),'events':[event()]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert len(await download(client,'365scores',DAY,'America/Lima'))==1
        assert len(calls)==1
        for mode in ('blocked','redirect','invalid','timeout'):
            def fail(req):
                if mode=='timeout':raise httpx.ReadTimeout('test',request=req)
                return httpx.Response(403 if mode=='blocked' else 302 if mode=='redirect' else 200,
                    headers={'Location':'https://attacker.invalid'},text='No structured catalogue')
            async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
                with pytest.raises(CatalogueError):await download(client,'betano',DAY,'America/Lima')
    asyncio.run(case())


def native_calendar():
    return {'countries':[{'id':25,'name':'Peru'}],
        'competitions':[{'id':583,'countryId':25,'name':'Liga 1','nameForURL':'liga-1'}],
        'games':[{'id':4760814,'sportId':1,'competitionId':583,'startTime':'2026-10-11T13:00:00-05:00',
                  'homeCompetitor':{'id':10367,'name':'UTC Cajamarca','nameForURL':'utc'},
                  'awayCompetitor':{'id':8420,'name':'Sporting Cristal','nameForURL':'sporting-cristal'}}]}


def test_native_365_calendar_matches_observed_canonical_url_and_day():
    data=native_calendar()
    events=scores365_events(data,DAY,'America/Lima')
    assert events[0]['url']=='https://www.365scores.com/es/football/match/liga-1-583/sporting-cristal-utc-8420-10367-583#id=4760814'
    assert events[0]['home']=='UTC Cajamarca' and events[0]['away']=='Sporting Cristal'
    assert links.validate_events(events,DAY,provider='365scores')
    data['games'][0]['startTime']='2026-10-11T04:59:59Z'
    assert scores365_events(data,DAY,'America/Lima')==[]
    data=native_calendar();data['games'][0]['homeCompetitor'].pop('nameForURL')
    with pytest.raises(CatalogueError,match='schema_changed'):scores365_events(data,DAY,'America/Lima')


def test_native_365_bulk_download_uses_single_day_and_one_call(monkeypatch):
    monkeypatch.delenv('V6_365SCORES_CATALOGUE_URL',raising=False)
    async def case():
        calls=[]
        def handler(req):
            calls.append(req)
            assert req.url.host=='webws.365scores.com' and req.url.path=='/web/games/'
            assert req.url.params['startDate']==req.url.params['endDate']=='11/10/2026'
            assert req.url.params['timezoneName']=='America/Lima' and req.url.params['sports']=='1'
            return httpx.Response(200,json=native_calendar())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            events=await download(client,'365scores',DAY,'America/Lima')
        assert len(calls)==1 and len(events)==1
    asyncio.run(case())


def test_alert_links_survive_all_telegram_formats():
    s={'id':1001,'home_team_name':'Local','away_team_name':'Visitante','home_score':0,'away_score':0,'snapshot_minute':75}
    p={'snapshot':s,'prediction':{'probability':.5},'fixture_links':{
        provider:{'url':event(provider)['url'],'event_id':'7001'} for provider in links.PROVIDERS}}
    for logos in (0,1,2):
        if logos>=1:s['home_team_logo']='https://cdn.sportmonks.com/1.png'
        if logos==2:s['away_team_logo']='https://cdn.sportmonks.com/2.png'
        method,body=telegram.request_body(p,'test-chat')
        text=body.get('text') or body.get('caption') or body['media'][0]['caption']
        assert 'Ver partido en 365Scores' in text and 'Abrir mercados en Betano' in text
        assert 'href="https://' in text
    p['fixture_links']['betano']['url']='https://attacker.invalid'
    assert 'Abrir mercados en Betano' not in telegram.format_message(p)


def run_db(fn):
    assert os.environ['V6_DATABASE_URL'].endswith('/sportedge_v6_test')
    async def case():
        p=await pool()
        try:
            async with p.acquire() as c:
                await c.execute('TRUNCATE v6.fixture_link_registry,v6.fixture_link_catalogues,v6.fixture_link_aliases,v6.fixture_link_runs')
            await fn(p)
        finally:await p.close()
    asyncio.run(case())


def test_web_catalogue_publish_preserves_started_events_and_operator_imports():
    today=datetime.now(ZoneInfo('America/Lima')).date()
    kickoff=links.day_bounds(today)[0]+timedelta(hours=12)
    first=event('betano',event_id='7001',starting_at=kickoff.isoformat())
    second=event('betano',event_id='7002',starting_at=kickoff.isoformat(),home='Segundo Local')
    async def case(p):
        assert await publish(p,today,[first])==1
        assert await publish(p,today,[second])==2
        async with p.acquire() as c:
            row=await c.fetchrow("SELECT * FROM v6.fixture_link_catalogues WHERE match_day=$1 AND provider='betano'",today)
            assert row['origin']==ORIGIN
            assert {e['event_id'] for e in row['events']}=={'7001','7002'}
            assert await c.fetchval("SELECT last_error FROM v6.fixture_link_runs WHERE match_day=$1 AND provider='betano'",today)=='catalogue_imported_rebuild_pending'
            await c.execute("UPDATE v6.fixture_link_catalogues SET origin='operator_import' WHERE match_day=$1",today)
        with pytest.raises(ValueError,match='operator_catalogue_preserved'):
            await publish(p,today,[first])
        async with p.acquire() as c:
            assert await c.fetchval('SELECT jsonb_array_length(events) FROM v6.fixture_link_catalogues WHERE match_day=$1',today)==2
    run_db(case)


def test_builder_bulk_cache_expiry_identity_and_retry(monkeypatch):
    today=datetime.now(timezone.utc).astimezone(ZoneInfo('America/Lima')).date()
    kickoff=links.day_bounds(today)[0]+timedelta(hours=12)
    async def source(day):
        assert day==today
        return [fixture(kickoff.isoformat()),fixture(kickoff.isoformat(),fixture_id=1002,home='Other')]
    monkeypatch.setattr(link_worker,'source_fixtures',source)
    async def case(p):
        calls=[]
        def handler(req):
            calls.append(str(req.url))
            provider='betano' if 'betano' in str(req.url) else '365scores'
            return httpx.Response(200,json={'match_day':str(today),'events':[event(provider,starting_at=kickoff.isoformat())]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            summary=await link_worker.build_day(p,client,today)
            assert all(v['matched']==1 and v['fixtures']==2 for v in summary.values())
            assert len(calls)==2
            assert await link_worker.build_day(p,client,today)=={'status':'not_due'}
        async with p.acquire() as c:
            snap={'id':1001,'starting_at':kickoff.isoformat(),'home_team_id':10,'away_team_id':20,'league_id':30}
            assert set(await links.cached_links(c,snap))==set(links.PROVIDERS)
            assert await links.cached_links(c,snap | {'home_team_id':999})=={}
            assert await links.cached_links(c,snap | {'starting_at':(kickoff+timedelta(hours=1)).isoformat()})=={}
            await c.execute("UPDATE v6.fixture_link_registry SET valid_until=now()-interval '1 second'")
            assert await links.cached_links(c,snap)=={}
        def blocked(req):return httpx.Response(403)
        async with httpx.AsyncClient(transport=httpx.MockTransport(blocked)) as client:
            summary=await link_worker.build_day(p,client,today,force=True)
            assert all(v['error']=='catalogue_http_403' for v in summary.values())
        async with p.acquire() as c:
            assert await c.fetchval("SELECT count(*) FROM v6.fixture_link_registry WHERE status='provider_error'")==4
            assert await c.fetchval("SELECT count(*) FROM v6.fixture_link_runs WHERE status='failed'")==2
    run_db(case)


def test_imported_catalogues_no_http_and_source_duplicates_are_ambiguous(monkeypatch):
    async def source(day):return [fixture(),fixture(fixture_id=1002)]
    monkeypatch.setattr(link_worker,'source_fixtures',source)
    async def case(p):
        async with p.acquire() as c:
            for provider in links.PROVIDERS:
                await c.execute('INSERT INTO v6.fixture_link_catalogues(match_day,provider,events,origin,valid_until) VALUES ($1,$2,$3,$4,now()+interval \'1 day\')',DAY,provider,[event(provider)],'test-import')
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:pytest.fail('Imported catalogue must not make HTTP requests'))) as client:
            result=await link_worker.build_day(p,client,DAY)
            assert all(v['matched']==0 for v in result.values())
        async with p.acquire() as c:
            assert await c.fetchval("SELECT count(*) FROM v6.fixture_link_registry WHERE status='ambiguous'")==4
    run_db(case)


def test_provider_failure_preserves_valid_cache_and_changed_identity_invalidates(monkeypatch):
    today=datetime.now(timezone.utc).astimezone(ZoneInfo('America/Lima')).date()
    kickoff=links.day_bounds(today)[0]+timedelta(hours=12)
    fixtures=[fixture(kickoff.isoformat())]
    async def source(day):return fixtures
    monkeypatch.setattr(link_worker,'source_fixtures',source)
    async def case(p):
        def handler(req):
            provider='betano' if 'betano' in str(req.url) else '365scores'
            return httpx.Response(200,json={'match_day':str(today),'events':[event(provider,starting_at=kickoff.isoformat())]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:await link_worker.build_day(p,client,today)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(403))) as client:
            result=await link_worker.build_day(p,client,today,True)
            assert all(v['matched']==1 for v in result.values())
            fixtures[0]['home_team_id']=999
            result=await link_worker.build_day(p,client,today,True)
            assert all(v['matched']==0 for v in result.values())
    run_db(case)


def test_telegram_claim_persists_links_without_provider_http(monkeypatch):
    from integration import repository as repo
    from test_integration import snapshot,result
    today=datetime.now(timezone.utc).astimezone(ZoneInfo('America/Lima')).date()
    kickoff=links.day_bounds(today)[0]+timedelta(hours=12)
    async def case(p):
        async with p.acquire() as c:
            await c.execute('TRUNCATE v6.telegram_outbox,v6.invocation_events,v6.match_centre_invocations,v6.collector_cursor')
            await c.execute("INSERT INTO v6.fixture_link_runs(match_day,provider,status) VALUES ($1,'betano','running')",today)
        s=snapshot()
        s.update(starting_at=kickoff.isoformat(),home_team_id=10,away_team_id=20,league_id=30)
        f=fixture(kickoff.isoformat(),fixture_id=s['id'],home_team_id=s['home_team_id'],away_team_id=s['away_team_id'],league_id=s['league_id'])
        await link_worker.save_registry(p,today,'betano',[f],[event('betano',starting_at=kickoff.isoformat())])
        job,_=await repo.submit(p,s);j=await repo.claim(p,job);r=result(j)
        await repo.complete(p,j,r,{'snapshot':s,'prediction':r})
        row=await telegram.claim(p)
        assert row['payload']['fixture_links']['betano']['event_id']=='7001'
        async with p.acquire() as c:
            payload=await c.fetchval('SELECT payload FROM v6.telegram_outbox WHERE outbox_id=$1',row['outbox_id'])
        assert payload['snapshot']==s and payload['fixture_links']==row['payload']['fixture_links']
    run_db(case)


@pytest.mark.parametrize('mode',['text','photo','album','fallback'])
def test_telegram_delivery_preserves_both_cached_links(monkeypatch,mode):
    from integration import repository as repo
    from test_integration import snapshot,result
    monkeypatch.setenv('V6_TELEGRAM_ENABLED','true')
    monkeypatch.setenv('V6_TELEGRAM_BOT_TOKEN','test-token')
    monkeypatch.setenv('V6_TELEGRAM_CHAT_ID','test-chat')
    monkeypatch.delenv('V6_TELEGRAM_BOT_TOKEN_FILE',raising=False)
    monkeypatch.delenv('V6_TELEGRAM_CHAT_ID_FILE',raising=False)
    today=datetime.now(ZoneInfo('America/Lima')).date()
    kickoff=links.day_bounds(today)[0]+timedelta(hours=12)
    async def case(p):
        async with p.acquire() as c:
            await c.execute('TRUNCATE v6.telegram_outbox,v6.invocation_events,v6.match_centre_invocations,v6.collector_cursor')
            for provider in links.PROVIDERS:
                await c.execute("INSERT INTO v6.fixture_link_runs(match_day,provider,status) VALUES ($1,$2,'running')",today,provider)
        s=snapshot()
        s.update(starting_at=kickoff.isoformat(),home_team_id=10,away_team_id=20,league_id=30)
        if mode!='text':s['home_team_logo']='https://cdn.sportmonks.com/1.png'
        if mode in ('album','fallback'):s['away_team_logo']='https://cdn.sportmonks.com/2.png'
        f=fixture(kickoff.isoformat(),fixture_id=s['id'])
        expected={provider:event(provider,starting_at=kickoff.isoformat()) for provider in links.PROVIDERS}
        for provider,e in expected.items():
            await link_worker.save_registry(p,today,provider,[f],[e])
        job,_=await repo.submit(p,s);j=await repo.claim(p,job);r=result(j)
        await repo.complete(p,j,r,{'snapshot':s,'prediction':r})
        methods=[]
        def handler(request):
            assert request.url.host=='api.telegram.org','Delivery must not call catalogue providers'
            method=request.url.path.rsplit('/',1)[-1];methods.append(method)
            body=json.loads(request.content)
            text=body['media'][0]['caption'] if method=='sendMediaGroup' else body.get('caption',body.get('text'))
            assert body['chat_id']=='test-chat'
            assert all(f'href="{e["url"]}"' in text for e in expected.values())
            if mode=='fallback' and len(methods)==1:
                return httpx.Response(400,json={'ok':False})
            messages=[{'message_id':301},{'message_id':302}] if method=='sendMediaGroup' else {'message_id':303}
            return httpx.Response(200,json={'ok':True,'result':messages})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert await telegram.deliver_one(p,client)
            assert not await telegram.deliver_one(p,client)
        async with p.acquire() as c:
            row=await c.fetchrow('SELECT * FROM v6.telegram_outbox')
        assert row['status']=='sent' and row['payload']['snapshot']==s
        assert set(row['payload']['fixture_links'])==set(links.PROVIDERS)
        assert all(row['payload']['fixture_links'][provider]['url']==e['url'] for provider,e in expected.items())
        assert methods=={'text':['sendMessage'],'photo':['sendPhoto'],
            'album':['sendMediaGroup'],'fallback':['sendMediaGroup','sendMessage']}[mode]
        assert row['telegram_message_id']==('301,302' if mode=='album' else '303')
    run_db(case)


def test_cache_query_failure_does_not_cancel_outbox_claim(monkeypatch):
    from integration import repository as repo
    from test_integration import snapshot,result
    async def broken(conn,snapshot):
        await conn.execute('SELECT 1/0')
    monkeypatch.setattr(telegram,'cached_links',broken)
    async def case(p):
        async with p.acquire() as c:await c.execute('TRUNCATE v6.telegram_outbox,v6.invocation_events,v6.match_centre_invocations,v6.collector_cursor')
        s=snapshot();job,_=await repo.submit(p,s);j=await repo.claim(p,job);r=result(j)
        await repo.complete(p,j,r,{'snapshot':s,'prediction':r})
        row=await telegram.claim(p)
        assert row and row['status']=='sending' and row['payload']['fixture_links']=={}
    run_db(case)


def test_catalogue_api_auth_validation_persistence_and_rebuild(monkeypatch):
    run_db(lambda p:asyncio.sleep(0))
    monkeypatch.setenv('V6_INTEGRATION_API_KEY','links-test-key')
    from integration.api import app
    headers={'Authorization':'Bearer links-test-key'}
    body={'match_day':str(DAY),'events':[event('betano')]}
    with TestClient(app) as client:
        assert client.put('/v1/fixture-links/catalogues/betano',json=body).status_code==401
        assert client.put('/v1/fixture-links/catalogues/betano',json=body,headers=headers).json()['recorded']
        invalid={'match_day':str(DAY),'events':[event('betano',url='https://attacker.invalid')]}
        assert client.put('/v1/fixture-links/catalogues/betano',json=invalid,headers=headers).status_code==422
        assert client.put('/v1/fixture-links/catalogues/other',json=body,headers=headers).status_code==404
        assert client.put('/v1/fixture-links/aliases/betano',json={'entity':'team','source_id':10,'provider_name':'Local FC'},headers=headers).status_code==200
        assert client.post('/v1/fixture-links/rebuild?match_day='+str(DAY),headers=headers).status_code==202
        response=client.get('/v1/fixture-links?match_day='+str(DAY),headers=headers).json()
        assert len(response['runs'])==2
        assert client.get('/v1/fixture-links/status').status_code==401
        status=client.get('/v1/fixture-links/status?match_day='+str(DAY),headers=headers).json()
        assert status['schedule']['component']=='v6.fixture_catalogues'
        assert status['schedule']['daily_start']=='02:00'
        assert status['schedule']['services']==['links-worker','betano-web']
        assert len(status['runs'])==2 and status['catalogues'][0]['event_count']==1
        assert status['provider_coverage']['betano']=='partial_featured_events'
