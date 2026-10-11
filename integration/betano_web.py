"""Bounded, anonymous public-calendar collection in a separate browser process.

No login, user profile, betslip, selections, proxy or anti-bot modifications.
Browser dependencies are optional and never imported by inference/Telegram.
"""
import argparse
import asyncio
from datetime import date, datetime, timedelta, timezone
import logging
import json
import os
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from integration.config import bounded
from integration.database import pool
from integration.fixture_links import day_bounds, validate_events
from integration.link_schedule import configuration,due_day

URL = 'https://www.betano.pe/sport/futbol/proximos-partidos-hoy/'
ORIGIN = 'betano_public_browser'
log = logging.getLogger(__name__)

# DOM fields observed in Betano's public calendar on 2026-10-11. Read only.
READ_ROWS = """() => ({
  rows: Array.from(document.querySelectorAll('a[data-qa="pre-event"]')).map(a => {
    const card = a.closest('[data-qa="event-card"]');
    const participants = a.querySelector('[data-qa="participants"]');
    return {event_id:card?.getAttribute('data-evtid'), href:a.getAttribute('href'),
      teams:participants ? Array.from(participants.children).map(e=>e.textContent.trim()) : [],
      competition:a.getAttribute('title'),
      clock:Array.from(card?.querySelectorAll('[data-qa="upcoming-event-card-date-time"] span') || []).map(e=>e.textContent.trim())};
  }),
  top:document.documentElement.scrollTop,
  height:document.documentElement.scrollHeight,
  viewport:document.documentElement.clientHeight,
  calendar:!!document.querySelector('[data-qa="upcoming-v2-events"]')
})"""

# Country labels are translated literally, independently of team matching.
COUNTRIES = {'Inglaterra':'England','España':'Spain','Italia':'Italy','Alemania':'Germany',
    'Francia':'France','Portugal':'Portugal','Perú':'Peru','Brasil':'Brazil',
    'Argentina':'Argentina','Chile':'Chile','Colombia':'Colombia','Ecuador':'Ecuador',
    'Uruguay':'Uruguay','Paraguay':'Paraguay','México':'Mexico','EE. UU.':'USA',
    'EE.\u00a0UU.':'USA','Países Bajos':'Netherlands','Bélgica':'Belgium',
    'Suiza':'Switzerland','Austria':'Austria','Dinamarca':'Denmark','Grecia':'Greece',
    'Turquía':'Turkey','Escocia':'Scotland','Gales':'Wales','Irlanda':'Republic of Ireland'}


def calendar_events(rows, day):
    """Do not infer names, IDs, slugs or kickoff from odds/bet buttons."""
    events = {}
    for row in rows:
        try:
            if len(row['teams']) != 2 or len(row['clock']) != 2:
                raise ValueError('missing_participants_or_clock')
            if row['clock'][0] != day.strftime('%d/%m'):
                raise ValueError('calendar_day_mismatch')
            clock = datetime.strptime(row['clock'][1], '%H:%M').time()
            country, league = row['competition'].split(' - ', 1)
            parsed = urlsplit(urljoin(URL, row['href']))
            # Discard browser-added analytics parameters; keep the published path.
            url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))
            event = {'event_id':row['event_id'], 'home':row['teams'][0], 'away':row['teams'][1],
                'league':league,'country':COUNTRIES.get(country,country),'url':url,
                'starting_at':datetime.combine(day,clock,ZoneInfo('America/Lima')).isoformat()}
            event = validate_events([event],day,provider='betano')[0]
            if event['event_id'] in events and events[event['event_id']] != event:
                raise ValueError('conflicting_calendar_event')
            events[event['event_id']] = event
        except (KeyError,TypeError,ValueError,AttributeError) as exc:
            raise ValueError('betano_invalid_calendar_row') from exc
    if not events:
        raise ValueError('betano_empty_calendar')
    return validate_events(list(events.values()),day,provider='betano')


def top_events(body,day):
    """Featured events, not a complete calendar. Ignore markets and selections."""
    try:
        catalogue=body['data']['topEventsV2']
        events=[]
        start,end=day_bounds(day)
        for key,event in catalogue['events'].items():
            if event['sportId']!='FOOT':continue
            if str(event['id'])!=key:raise ValueError('event_key_mismatch')
            kickoff=datetime.fromtimestamp(event['startTime']/1000,timezone.utc)
            if not start<=kickoff<end:continue
            participants=event['participants']
            if len(participants)!=2:raise ValueError('invalid_participants')
            country=catalogue['zones'][str(event['zoneId'])]['name']
            events.append({'event_id':key,'starting_at':kickoff.isoformat(),
                'home':participants[0]['name'],'away':participants[1]['name'],
                'league':catalogue['leagues'][str(event['leagueId'])]['name'],
                'country':COUNTRIES.get(country,country),'url':urljoin(URL,event['url'])})
        if not events:raise ValueError('no_featured_football_today')
        return validate_events(events,day,provider='betano')
    except (KeyError,TypeError,ValueError,AttributeError,OverflowError,OSError) as exc:
        raise ValueError('betano_featured_schema_unavailable') from exc


async def collect(day):
    """One ordinary page load; read its already received anonymous bulk response."""
    if os.getenv('V6_BETANO_WEB_MODE','featured')=='calendar':return await collect_calendar(day)
    from playwright.async_api import async_playwright
    if day!=datetime.now(ZoneInfo('America/Lima')).date():raise ValueError('browser_current_day_only')
    async with async_playwright() as pw:
        browser=await pw.firefox.launch(headless=True)
        try:
            context=await browser.new_context(locale='es-PE',timezone_id='America/Lima',
                viewport={'width':1280,'height':900},accept_downloads=False)
            async def resources(route):
                if route.request.resource_type in ('image','media','font'):await route.abort()
                else:await route.continue_()
            await context.route('**/*',resources)
            page=await context.new_page()
            async with page.expect_response(lambda r:urlsplit(r.url).hostname=='www.betano.pe'
                and urlsplit(r.url).path=='/api/home/top-events-v2/',timeout=30000) as received:
                response=await page.goto(URL,wait_until='domcontentloaded',timeout=20000)
                if not response or response.status!=200:raise ValueError('betano_public_page_unavailable')
            response=await received.value
            if response.status!=200:raise ValueError('betano_featured_http_'+str(response.status))
            data=await response.body()
            if len(data)>4*1024*1024:raise ValueError('betano_featured_too_large')
            return top_events(json.loads(data),day)
        finally:await browser.close()


async def collect_calendar(day):
    # Imported only inside this optional service, with a fresh anonymous profile.
    from playwright.async_api import async_playwright
    if day != datetime.now(ZoneInfo('America/Lima')).date():
        raise ValueError('browser_calendar_current_day_only')
    async with async_playwright() as pw:
        browser = await pw.firefox.launch(headless=True)
        try:
            context = await browser.new_context(locale='es-PE',timezone_id='America/Lima',
                viewport={'width':1280,'height':900},accept_downloads=False)
            async def resources(route):
                if route.request.resource_type in ('image','media','font'):
                    await route.abort()
                else:await route.continue_()
            await context.route('**/*',resources)
            page = await context.new_page()
            responses = []
            failures = []
            def response_status(r):
                parsed = urlsplit(r.url)
                if parsed.hostname=='www.betano.pe' and parsed.path.startswith('/api/'):
                    responses.append({'path':parsed.path,'status':r.status})
            page.on('response',response_status)
            page.on('requestfailed',lambda r:failures.append({'host':urlsplit(r.url).hostname,'failure':r.failure}))
            page.set_default_timeout(30000)
            response = await page.goto(URL,wait_until='domcontentloaded',timeout=20000)
            if not response or response.status != 200:
                raise ValueError('betano_browser_http_'+str(response.status if response else 'missing'))
            reject = page.get_by_role('button',name='Rechazar Todas',exact=True)
            log.info('Betano public calendar loaded: status=%s',response.status)
            if await reject.count() and await reject.is_visible():await reject.click()
            try:
                await page.get_by_role('button',name='HOY',exact=True).click()
            except Exception:
                log.warning('Public calendar not rendered: title=%s api_responses=%s failed_resources=%s',
                    await page.title(),responses[:12],failures[:12])
                raise
            await page.get_by_role('radio',name='Hora de inicio',exact=True).check()
            await page.locator('[data-qa="pre-event"]').first.wait_for()
            captured = {}
            stable_bottom = 0
            for _ in range(120):
                state = await page.evaluate(READ_ROWS)
                if not state['calendar']:raise ValueError('betano_calendar_schema_changed')
                for event in calendar_events(state['rows'],day):
                    old = captured.get(event['event_id'])
                    if old and old != event:raise ValueError('betano_calendar_changed_during_read')
                    captured[event['event_id']] = event
                if len(captured)>10000:raise ValueError('betano_calendar_event_limit')
                bottom = state['top']+state['viewport'] >= state['height']-2
                stable_bottom = stable_bottom+1 if bottom else 0
                if stable_bottom>=3:return list(captured.values())
                # Half a viewport overlaps the recycled rows and cannot skip a block.
                await page.mouse.wheel(0,max(200,state['viewport']//2))
                await asyncio.sleep(.25)
            raise ValueError('betano_calendar_scroll_limit')
        finally:await browser.close()


async def publish(p,day,events):
    events = validate_events(events,day,provider='betano')
    async with p.acquire() as c, c.transaction():
        if not await c.fetchval('SELECT pg_try_advisory_xact_lock(610104)'):
            raise ValueError('link_builder_busy')
        prior = await c.fetchrow('SELECT events,origin FROM v6.fixture_link_catalogues WHERE match_day=$1 AND provider=\'betano\'',day)
        if prior and prior['origin'] != ORIGIN:
            raise ValueError('operator_catalogue_preserved')
        # Refreshes retain today's already-started events absent from Upcoming.
        merged = {e['event_id']:e for e in (prior['events'] if prior else [])}
        merged.update({e['event_id']:e for e in events})
        merged = validate_events(list(merged.values()),day,provider='betano')
        await c.execute('''INSERT INTO v6.fixture_link_catalogues(match_day,provider,events,origin,valid_until)
            VALUES ($1,'betano',$2,$3,$4) ON CONFLICT(match_day,provider) DO UPDATE
            SET events=EXCLUDED.events,origin=EXCLUDED.origin,valid_until=EXCLUDED.valid_until,imported_at=now()''',
            day,merged,ORIGIN,day_bounds(day)[1]+timedelta(hours=12))
        await c.execute('''INSERT INTO v6.fixture_link_runs(match_day,provider,status,attempts,last_error)
            VALUES ($1,'betano','failed',0,'catalogue_imported_rebuild_pending')
            ON CONFLICT(match_day,provider) DO UPDATE SET status='failed',attempts=0,
            next_attempt_at=now(),last_error='catalogue_imported_rebuild_pending' ''',day)
    return len(merged)


async def run(once=False):
    log.info('V6 fixture catalogues started: service=betano-web configuration=%s',configuration())
    p = await pool()
    next_attempt = datetime.min.replace(tzinfo=timezone.utc)
    attempts = 0
    active_day = None
    try:
        while True:
            day = due_day()
            if day != active_day:
                active_day,attempts = day,0
                next_attempt = datetime.min.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            if day and now>=next_attempt and attempts<4:
                async with p.acquire() as c:
                    catalogue = await c.fetchrow('SELECT origin,imported_at FROM v6.fixture_link_catalogues WHERE match_day=$1 AND provider=\'betano\'',day)
                refresh = int(bounded('V6_LINK_REFRESH_SECONDS',21600,900,86400))
                if catalogue and (catalogue['origin']!=ORIGIN or catalogue['imported_at']+timedelta(seconds=refresh)>now):
                    next_attempt = now+timedelta(seconds=refresh)
                else:
                    try:
                        events = await asyncio.wait_for(collect(day),180)
                        count = await publish(p,day,events)
                        log.info('Betano public browser catalogue: day=%s events=%s',day,count)
                        attempts = 0
                        next_attempt = now+timedelta(seconds=refresh)
                    except Exception as exc:
                        attempts += 1
                        next_attempt = now+timedelta(minutes=15)
                        # No browser dumps, cookies, credentials or account state in logs.
                        reason = str(exc) if isinstance(exc,ValueError) else type(exc).__name__
                        log.warning('Betano public browser failed: day=%s attempt=%s reason=%s',day,attempts,reason[:120])
                        if once:raise
            if once:return
            await asyncio.sleep(60)
    finally:await p.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(args.once))
