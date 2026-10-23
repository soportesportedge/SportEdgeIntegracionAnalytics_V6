import asyncio
from datetime import datetime,timedelta,timezone
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import httpx
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from integration import link_worker,betano_web,fixture_links as links
from integration.link_jobs import select_fixtures,request_betano,claim_browser_request
from integration.link_schedule import slot_at,next_slot,scheduled_days,configuration
from test_fixture_links import run_db,fixture,event


@pytest.fixture(autouse=True)
def two_hour_schedule(monkeypatch):
    monkeypatch.setenv('V6_LINK_BUILD_HOUR','2')
    monkeypatch.setenv('V6_LINK_BUILD_MINUTE','0')
    monkeypatch.setenv('V6_LINK_REFRESH_SECONDS','7200')


def local(day,hour=0,minute=0):
    return datetime.combine(day,datetime.min.time(),ZoneInfo('America/Lima')).replace(hour=hour,minute=minute).astimezone(timezone.utc)


def test_slots_midnight_and_window_days():
    day=datetime.now(ZoneInfo('America/Lima')).date()
    assert slot_at(local(day,1,59))==local(day,0)
    assert next_slot(local(day,1,59))==local(day,2)
    assert slot_at(local(day,4,37))==local(day,4)
    assert next_slot(local(day,23,59))==local(day+timedelta(days=1),0)
    assert scheduled_days(local(day,0))==[day-timedelta(days=1),day]
    assert scheduled_days(local(day,22))==[day,day+timedelta(days=1)]
    config=configuration(local(day,4,37))
    assert config['refresh_seconds']==7200 and config['refresh_mode']=='unresolved_only'
    assert config['incremental_past_hours']==1 and config['incremental_future_hours']==2


def test_incremental_boundaries_expiry_provider_identity_and_full_day():
    now=datetime.now(timezone.utc)
    offsets=(-3601,-3600,0,7200,7201)
    fixtures=[fixture((now+timedelta(seconds=offset)).isoformat(),fixture_id=i+1) for i,offset in enumerate(offsets)]
    assert [f['fixture_id'] for f in select_fixtures(fixtures,[],'incremental',now)]==[2,3,4]
    assert select_fixtures(fixtures,[],'full',now)==fixtures
    valid=fixtures[2]|{'fixture_id':3,'provider':'365scores','status':'matched','event_id':'7001',
        'url':event()['url'],'starting_at':now,'valid_until':now+timedelta(hours=1)}
    assert [f['fixture_id'] for f in select_fixtures(fixtures,[valid],'incremental',now)]==[2,4]
    for changed in ({'valid_until':now-timedelta(seconds=1)},{'home_team_id':999},
                    {'url':'https://attacker.invalid'},{'status':'not_found'}):
        assert [f['fixture_id'] for f in select_fixtures(fixtures,[valid|changed],'incremental',now)]==[2,3,4]


def test_full_2am_then_only_unresolved_nearby_and_no_download_when_empty(monkeypatch):
    monkeypatch.setenv('V6_BETANO_CATALOGUE_URL','https://www.betano.pe/test-feed')
    day=datetime.now(ZoneInfo('America/Lima')).date()
    full,incremental=local(day,2),local(day,4)
    starts=[local(day,20),local(day,5),local(day,3),local(day,3)-timedelta(seconds=1),local(day,6)+timedelta(seconds=1)]
    fixtures=[fixture(k.isoformat(),fixture_id=1001+i,home='Home '+str(i),home_team_id=10+i) for i,k in enumerate(starts)]
    source_calls=[]
    async def source(requested):
        assert requested==day
        source_calls.append(requested)
        return fixtures
    monkeypatch.setattr(link_worker,'source_fixtures',source)

    async def case(p):
        calls=[];phase=[0]
        def handler(request):
            provider='betano' if 'betano' in request.url.host else '365scores'
            calls.append((provider,phase[0]))
            subset=fixtures[:1] if phase[0]==0 else fixtures[1:]
            events=[event(provider,event_id=str(f['fixture_id']),home=f['home'],starting_at=f['starting_at'].isoformat()
                          if isinstance(f['starting_at'],datetime) else f['starting_at']) for f in subset]
            return httpx.Response(200,json={'match_day':str(day),'events':events})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result=await link_worker.build_day(p,client,day,now=full,scheduled=True)
            assert all(v['mode']=='full' and v['candidates']==5 and v['matched']==1 for v in result.values())
            async with p.acquire() as c:
                known=await c.fetch('SELECT * FROM v6.fixture_link_registry WHERE fixture_id=1001 ORDER BY provider')
                # Align stored download time with this simulated 02:00 batch.
                await c.execute('UPDATE v6.fixture_link_catalogues SET imported_at=$1',full)
            phase[0]=1
            result=await link_worker.build_day(p,client,day,now=incremental,scheduled=True)
            assert all(v['mode']=='incremental' and v['candidates']==2 and v['matched']==2 for v in result.values())
            async with p.acquire() as c:
                assert await c.fetch('SELECT * FROM v6.fixture_link_registry WHERE fixture_id=1001 ORDER BY provider')==known
                assert await c.fetchval("SELECT count(*) FROM v6.fixture_link_registry WHERE status='matched'")==6
                assert await c.fetchval("SELECT count(*) FROM v6.fixture_link_runs WHERE candidate_count=2 AND matched_count=3 AND preserved_count=1")==2
            assert await link_worker.build_day(p,client,day,now=incremental+timedelta(minutes=1),scheduled=True)=={'status':'not_due'}
            # At 08:00 the remaining unresolved games are older than one hour.
            result=await link_worker.build_day(p,client,day,now=local(day,8),scheduled=True)
            assert all(v['mode']=='incremental' and v['candidates']==0 for v in result.values())
            assert len(calls)==4
            assert len(source_calls)==3
    run_db(case)


def test_betano_browser_is_requested_only_for_missing_provider_and_consumed_once(monkeypatch):
    monkeypatch.delenv('V6_BETANO_CATALOGUE_URL',raising=False)
    day=datetime.now(ZoneInfo('America/Lima')).date()
    now=local(day,4)
    f=fixture(local(day,5).isoformat())
    async def source(requested):return [f]
    monkeypatch.setattr(link_worker,'source_fixtures',source)
    collections=[]
    async def collect(requested):
        collections.append(requested)
        return [event('betano',starting_at=f['starting_at'].isoformat())]
    monkeypatch.setattr(betano_web,'collect',collect)
    async def case(p):
        async with p.acquire() as c:
            for provider in links.PROVIDERS:
                await c.execute("INSERT INTO v6.fixture_link_runs(match_day,provider,status,full_completed_at,slot_at,next_attempt_at) VALUES ($1,$2,'completed',$3,$3,$3)",day,provider,local(day,2))
        await link_worker.save_registry(p,day,'365scores',[f],[event(starting_at=f['starting_at'].isoformat())],now=local(day,2))
        def no_http(request):pytest.fail('A valid 365Scores link must not trigger HTTP; Betano belongs to the browser')
        async with httpx.AsyncClient(transport=httpx.MockTransport(no_http)) as client:
            first=await link_worker.build_day(p,client,day,now=now,scheduled=True)
            assert first['365scores']['candidates']==0
            assert first['betano']['candidates']==1 and first['betano']['catalogue_pending']
            assert await link_worker.build_day(p,client,day,now=now+timedelta(minutes=1),scheduled=True)=={'status':'not_due'}
            assert await betano_web.process_request(p)
            assert not await betano_web.process_request(p)
            # The real DB clock may precede this simulated 04:00 slot.
            async with p.acquire() as c:
                await c.execute('UPDATE v6.fixture_link_catalogues SET imported_at=$1',now)
                await c.execute("UPDATE v6.fixture_link_runs SET next_attempt_at=$1 WHERE provider='betano'",now)
            second=await link_worker.build_day(p,client,day,now=now+timedelta(minutes=1),scheduled=True)
            assert second['betano']['matched']==1 and not second['betano']['catalogue_pending']
            assert collections==[day]
            async with p.acquire() as c:
                assert await c.fetchval("SELECT status FROM v6.fixture_link_catalogue_requests")=='completed'
                snap=f|{'id':f['fixture_id']}
                assert set(await links.cached_links(c,snap))==set(links.PROVIDERS)
            result=await link_worker.build_day(p,client,day,now=local(day,6),scheduled=True)
            assert all(v['candidates']==0 for v in result.values())
            assert not await betano_web.process_request(p)
    run_db(case)


def test_midnight_incremental_does_not_replace_2am_full_day(monkeypatch):
    monkeypatch.setenv('V6_BETANO_CATALOGUE_URL','https://www.betano.pe/test-feed')
    day=datetime.now(ZoneInfo('America/Lima')).date()
    f=fixture(local(day,0,30).isoformat())
    later=fixture(local(day,20).isoformat(),fixture_id=1002,home='Later')
    async def source(requested):return [f,later]
    monkeypatch.setattr(link_worker,'source_fixtures',source)
    async def case(p):
        def handler(request):
            provider='betano' if 'betano' in request.url.host else '365scores'
            return httpx.Response(200,json={'match_day':str(day),'events':[event(provider,starting_at=f['starting_at'].isoformat())]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result=await link_worker.build_day(p,client,day,now=local(day,0),scheduled=True)
            assert all(v['mode']=='incremental' and v['candidates']==1 for v in result.values())
            async with p.acquire() as c:
                assert await c.fetchval('SELECT count(*) FROM v6.fixture_link_runs WHERE full_completed_at IS NULL')==2
                await c.execute('UPDATE v6.fixture_link_catalogues SET imported_at=$1',local(day,0))
                # Even an explicit operator rebuild before 02:00 cannot replace the daily batch.
                await c.execute('UPDATE v6.fixture_link_runs SET full_completed_at=$1',local(day,0))
            result=await link_worker.build_day(p,client,day,now=local(day,2),scheduled=True)
            assert all(v['mode']=='full' and v['candidates']==2 for v in result.values())
            async with p.acquire() as c:
                assert await c.fetchval('SELECT count(*) FROM v6.fixture_link_runs WHERE full_completed_at IS NOT NULL')==2
    run_db(case)


def test_incremental_collision_does_not_steal_existing_event():
    day=datetime.now(ZoneInfo('America/Lima')).date()
    kickoff=local(day,5)
    f=fixture(kickoff.isoformat())
    async def case(p):
        async with p.acquire() as c:
            await c.execute("INSERT INTO v6.fixture_link_runs(match_day,provider,status) VALUES ($1,'betano','running')",day)
        e=event('betano',starting_at=kickoff.isoformat())
        await link_worker.save_registry(p,day,'betano',[f],[e])
        async with p.acquire() as c:
            before=await c.fetchrow('SELECT * FROM v6.fixture_link_registry WHERE fixture_id=1001')
        other=f|{'fixture_id':1002}
        assert await link_worker.save_registry(p,day,'betano',[other],[e],mode='incremental')==0
        async with p.acquire() as c:
            assert await c.fetchrow('SELECT * FROM v6.fixture_link_registry WHERE fixture_id=1001')==before
            row=await c.fetchrow('SELECT * FROM v6.fixture_link_registry WHERE fixture_id=1002')
            assert row['status']=='ambiguous' and row['url'] is None
    run_db(case)


def test_failed_browser_requests_are_bounded_and_new_slot_resets(monkeypatch):
    day=datetime.now(ZoneInfo('America/Lima')).date()
    now=datetime.now(timezone.utc)
    calls=[]
    async def failed(requested):
        calls.append(requested)
        raise ValueError('public_page_unavailable')
    monkeypatch.setattr(betano_web,'collect',failed)
    async def case(p):
        async with p.acquire() as c:await request_betano(c,day,slot_at(now))
        for _ in range(4):
            assert await betano_web.process_request(p)
            async with p.acquire() as c:
                await c.execute("UPDATE v6.fixture_link_catalogue_requests SET next_attempt_at=now()-interval '1 second'")
        assert not await betano_web.process_request(p)
        async with p.acquire() as c:
            with pytest.raises(ValueError,match='retries_exhausted'):
                await request_betano(c,day,slot_at(now))
            await request_betano(c,day,next_slot(now))
        assert await betano_web.process_request(p)
        assert len(calls)==5
    run_db(case)


def test_stale_browser_response_cannot_replace_new_request():
    day=datetime.now(ZoneInfo('America/Lima')).date()
    now=datetime.now(timezone.utc)
    async def case(p):
        async with p.acquire() as c:await request_betano(c,day,slot_at(now))
        stale=await claim_browser_request(p)
        async with p.acquire() as c:await request_betano(c,day,next_slot(now))
        with pytest.raises(ValueError,match='stale_catalogue_request'):
            await betano_web.publish(p,day,[event('betano',starting_at=local(day,12).isoformat())],stale['request_id'])
        async with p.acquire() as c:
            assert await c.fetchval('SELECT count(*) FROM v6.fixture_link_catalogues')==0
            assert await c.fetchval('SELECT status FROM v6.fixture_link_catalogue_requests')=='pending'
    run_db(case)


def test_failed_daily_batch_retries_only_nearby_fixtures_in_next_slot(monkeypatch):
    monkeypatch.setenv('V6_BETANO_CATALOGUE_URL','https://www.betano.pe/test-feed')
    day=datetime.now(ZoneInfo('America/Lima')).date()
    nearby=fixture(local(day,5).isoformat())
    distant=fixture(local(day,20).isoformat(),fixture_id=1002,home='Distant')
    async def source(requested):return [nearby,distant]
    monkeypatch.setattr(link_worker,'source_fixtures',source)
    async def case(p):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(503))) as client:
            first=await link_worker.build_day(p,client,day,now=local(day,2),scheduled=True)
            assert all(v['mode']=='full' and v['candidates']==2 and v['error']=='catalogue_http_503' for v in first.values())
            def handler(request):
                provider='betano' if 'betano' in request.url.host else '365scores'
                return httpx.Response(200,json={'match_day':str(day),'events':[event(provider,starting_at=nearby['starting_at'].isoformat())]})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as recovered:
                second=await link_worker.build_day(p,recovered,day,now=local(day,4),scheduled=True)
                assert all(v['mode']=='incremental' and v['candidates']==1 and v['matched']==1 for v in second.values())
            async with p.acquire() as c:
                assert await c.fetchval('SELECT count(*) FROM v6.fixture_link_runs WHERE full_completed_at IS NULL AND full_attempted_at IS NOT NULL')==2
                assert await c.fetchval("SELECT count(*) FROM v6.fixture_link_registry WHERE fixture_id=1002 AND status='provider_error'")==2
    run_db(case)
