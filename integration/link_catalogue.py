"""One bounded bulk download per provider. No event-page crawling or betting."""
from datetime import date
from html.parser import HTMLParser
import json
import os
from urllib.parse import quote, urlsplit
import httpx
from integration.config import secret
from integration.fixture_links import HOSTS, instant, day_bounds, validate_events

DEFAULT_URLS = {'365scores':'https://webws.365scores.com/web/games/', 'betano':'https://www.betano.pe'}
MAX_BYTES = 4 * 1024 * 1024


class CatalogueError(ValueError):
    pass


class StructuredData(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.parts = []
        self.scripts = []

    def handle_starttag(self,tag,attrs):
        if tag == 'script' and dict(attrs).get('type','').lower() == 'application/ld+json':
            self.active = True
            self.parts = []

    def handle_data(self,data):
        if self.active:
            self.parts.append(data)

    def handle_endtag(self,tag):
        if tag == 'script' and self.active:
            self.scripts.append(''.join(self.parts))
            self.active = False


def sports_events(text,day,tz):
    """Accept only published SportsEvent JSON-LD with an explicit event URL."""
    parser = StructuredData()
    parser.feed(text)
    output = []
    start,end = day_bounds(day,tz)
    def visit(node):
        if isinstance(node,list):
            for item in node: visit(item)
        elif isinstance(node,dict):
            if node.get('@type') == 'SportsEvent':
                try:
                    kickoff = instant(node['startDate'])
                    if start <= kickoff < end:
                        league = node.get('superEvent',{})
                        identifier = node.get('identifier')
                        if isinstance(identifier,dict): identifier = identifier.get('value')
                        output.append({'event_id':str(identifier or ''),'starting_at':kickoff.isoformat(),
                            'home':node['homeTeam']['name'],'away':node['awayTeam']['name'],
                            'league':league.get('name',''),'url':node.get('url',''),
                            'country':league.get('location',{}).get('name','')})
                except (KeyError,TypeError,ValueError,AttributeError):
                    raise CatalogueError('incomplete_published_event') from None
            else:
                for child in node.values():
                    if isinstance(child,(list,dict)): visit(child)
    try:
        for script in parser.scripts:visit(json.loads(script))
    except json.JSONDecodeError:
        raise CatalogueError('invalid_published_json') from None
    if not output:
        raise CatalogueError('public_catalogue_schema_unavailable')
    return output


def scores365_events(body,day,tz):
    """Public web calendar contract verified against the provider's actual app.

    URL slugs and IDs come exclusively from its metadata. The pairing path is
    ordered by competitor ID, and #id pins the specific game (not the pairing).
    """
    if not isinstance(body,dict) or not isinstance(body.get('games'),list):
        raise CatalogueError('365scores_schema_changed')
    try:
        competitions = {c['id']:c for c in body['competitions']}
        countries = {c['id']:c for c in body['countries']}
        start,end = day_bounds(day,tz)
        events = []
        for game in body['games']:
            if game['sportId']!=1:continue
            kickoff = instant(game['startTime'])
            if not start<=kickoff<end:continue
            home,away = game['homeCompetitor'],game['awayCompetitor']
            competition = competitions[game['competitionId']]
            first,second = sorted((home,away),key=lambda t:int(t['id']))
            def slug(entity):
                value = entity['nameForURL']
                if not isinstance(value,str) or not value or '/' in value or len(value)>200:
                    raise CatalogueError('365scores_invalid_slug')
                return quote(value,safe='.-')
            ids = [game['id'],competition['id'],home['id'],away['id']]
            if any(type(i) is not int or i<=0 for i in ids):
                raise CatalogueError('365scores_invalid_identity')
            country = countries.get(competition.get('countryId'),{})
            url = ('https://www.365scores.com/es/football/match/'+slug(competition)+'-'+str(competition['id'])
                +'/'+slug(first)+'-'+slug(second)+'-'+str(first['id'])+'-'+str(second['id'])
                +'-'+str(competition['id'])+'#id='+str(game['id']))
            events.append({'event_id':str(game['id']),'home':home['name'],'away':away['name'],
                'league':competition['name'],'country':country.get('name',''),
                'starting_at':kickoff.isoformat(),'url':url})
        if len(events)>10000:raise CatalogueError('365scores_event_limit')
        return events
    except (KeyError,TypeError,ValueError) as exc:
        if isinstance(exc,CatalogueError):raise
        raise CatalogueError('365scores_schema_changed') from None


async def download(client,provider,day:date,tz):
    prefix = 'V6_365SCORES' if provider == '365scores' else 'V6_BETANO'
    url = os.getenv(prefix+'_CATALOGUE_URL',DEFAULT_URLS[provider]).replace('{date}',day.isoformat())
    parsed = urlsplit(url)
    extra_hosts = {h.strip() for h in os.getenv('V6_LINK_FEED_ALLOWED_HOSTS','').split(',') if h.strip()}
    catalogue_hosts = HOSTS[provider] | extra_hosts | ({'webws.365scores.com'} if provider=='365scores' else set())
    if (parsed.scheme!='https' or parsed.hostname not in catalogue_hosts
            or parsed.username or parsed.password or parsed.port not in (None,443)):
        raise CatalogueError('catalogue_host_not_allowed')
    headers = {'Accept':'application/json, text/html'}
    token = secret(prefix+'_CATALOGUE_TOKEN')
    if token:headers['Authorization'] = 'Bearer '+token
    native365 = provider=='365scores' and parsed.hostname=='webws.365scores.com' and parsed.path=='/web/games/'
    params = {'appTypeId':5,'langId':1,'timezoneName':tz,'sports':1,
              'startDate':day.strftime('%d/%m/%Y'),'endDate':day.strftime('%d/%m/%Y')} if native365 else None
    try:
        async with client.stream('GET',url,params=params,headers=headers,timeout=httpx.Timeout(10,connect=3),follow_redirects=False) as response:
            if response.status_code!=200:
                raise CatalogueError('catalogue_http_'+str(response.status_code))
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data)>MAX_BYTES:raise CatalogueError('catalogue_too_large')
            is_json = 'json' in response.headers.get('content-type','')
        if is_json:
            body = json.loads(data)
            if native365 and isinstance(body,dict) and 'games' in body:
                events = scores365_events(body,day,tz)
            else:
                if not isinstance(body,dict) or body.get('match_day')!=day.isoformat():
                    raise CatalogueError('catalogue_day_mismatch')
                events = body.get('events')
        else:
            events = sports_events(data.decode('utf-8'),day,tz)
        return validate_events(events,day,tz,provider)
    except httpx.HTTPError as exc:
        raise CatalogueError('catalogue_'+type(exc).__name__) from None
    except (UnicodeError,json.JSONDecodeError):
        raise CatalogueError('catalogue_invalid_encoding') from None
