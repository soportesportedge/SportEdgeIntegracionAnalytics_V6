"""Pure event matching and local registry access; never makes external requests."""
from datetime import date, datetime, time, timedelta, timezone
import re
import unicodedata
from urllib.parse import parse_qs, unquote, urlsplit
from zoneinfo import ZoneInfo

PROVIDERS = ('365scores', 'betano')
HOSTS = {'365scores': {'www.365scores.com', '365scores.com'},
         'betano': {'www.betano.pe', 'betano.pe'}}


def normalise(value):
    value = unicodedata.normalize('NFKD', str(value or '')).casefold()
    return ' '.join(re.sub(r'[^\w]+', ' ', ''.join(c for c in value if not unicodedata.combining(c))).split())


def instant(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('timezone_required')
    return result.astimezone(timezone.utc)


def day_bounds(day: date, tz='America/Lima'):
    zone = ZoneInfo(tz)
    return (datetime.combine(day, time.min, zone).astimezone(timezone.utc),
            datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(timezone.utc))


def valid_event_url(provider, value, event_id=None):
    """Accept only HTTPS event pages, never home/team/search/league links."""
    if provider not in HOSTS or not isinstance(value, str) or len(value) > 1500:
        return False
    if any(c.isspace() or ord(c) < 32 for c in value) or '\\' in value:
        return False
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or parsed.hostname not in HOSTS[provider]
                or parsed.username or parsed.password or parsed.port not in (None,443)):
            return False
    except ValueError:
        return False
    path = unquote(parsed.path)
    if provider == '365scores':
        is_event = bool(re.fullmatch(r'/es(?:-[a-z]{2})?/football/match/[^/]+/[^/]+/?', path))
    else:
        is_event = bool(re.fullmatch(r'/(?:cuotas-de-partido|odds)/[^/]+/[0-9]+/?', path))
    if not is_event:
        return False
    # The path in 365Scores can identify a pairing. Require the specific game ID.
    if provider == '365scores' and event_id is not None:
        ids = parse_qs(parsed.query).get('gameId',[]) + parse_qs(parsed.fragment).get('id',[])
        return ids == [str(event_id)]
    if provider == 'betano' and event_id is not None:
        return path.rstrip('/').rsplit('/',1)[-1] == str(event_id)
    return True


def validate_events(events, day, tz='America/Lima', provider=None):
    if provider not in PROVIDERS or not isinstance(events,list) or len(events)>10000:
        raise ValueError('invalid_catalogue')
    start, end = day_bounds(day,tz)
    seen = set()
    output = []
    for event in events:
        if not isinstance(event,dict):
            raise ValueError('invalid_event')
        fields = ('event_id','home','away','league','url')
        if any(not isinstance(event.get(k),str) or not event[k].strip() or len(event[k])>1500 for k in fields):
            raise ValueError('missing_event_identity')
        if not valid_event_url(provider,event['url'],event['event_id']):
            raise ValueError('invalid_event_url')
        kickoff = instant(event.get('starting_at'))
        if not start <= kickoff < end:
            raise ValueError('event_outside_match_day')
        if event['event_id'] in seen:
            raise ValueError('duplicate_event_id')
        seen.add(event['event_id'])
        output.append({k:event[k] for k in fields} | {'starting_at':kickoff.isoformat(),
                       'country':str(event.get('country') or '')[:200]})
    return output


def identity(fixture, aliases):
    def name(entity, field, source_id):
        if source_id is None:return ''
        return normalise(aliases.get((entity,int(source_id)), fixture.get(field)))
    return (name('team','home',fixture['home_team_id']),
            name('team','away',fixture['away_team_id']),
            name('league','league',fixture['league_id']))


def event_index(events):
    index = {}
    for event in events:
        key = tuple(normalise(event[k]) for k in ('home','away','league'))
        index.setdefault(key,[]).append(event)
    return index


def resolve(fixture,index,aliases=None,tolerance_seconds=600):
    key = identity(fixture,aliases or {})
    if not all(key):
        return {'status':'not_found','evidence':{'reason':'missing_source_identity'}}
    candidates = [e for e in index.get(key,[]) if abs((instant(e['starting_at'])-instant(fixture['starting_at'])).total_seconds())<=tolerance_seconds
                  and (not fixture.get('country') or not e.get('country') or normalise(fixture['country'])==normalise(e['country']))]
    if len(candidates)!=1:
        return {'status':'ambiguous' if candidates else 'not_found',
                'evidence':{'candidate_ids':[e['event_id'] for e in candidates],'reason':'multiple_matches' if candidates else 'no_exact_match'}}
    event = candidates[0]
    return {'status':'matched','event_id':event['event_id'],'url':event['url'],
            'evidence':{'rule':'home_away_league_kickoff','event':event,
                        'source_names':{k:fixture.get(k) for k in ('home','away','league','country')},
                        'kickoff_delta_seconds':abs((instant(event['starting_at'])-instant(fixture['starting_at'])).total_seconds())}}


async def cached_links(conn,snapshot):
    """Indexed local read with strict fixture identity; no remote fallback."""
    if any(snapshot.get(k) is None for k in ('id','starting_at','home_team_id','away_team_id','league_id')):
        return {}
    try:
        kickoff = instant(snapshot['starting_at'])
        fixture_id, home_id, away_id, league_id = (int(snapshot[k]) for k in ('id','home_team_id','away_team_id','league_id'))
    except (ValueError,TypeError):
        return {}
    rows = await conn.fetch('''SELECT provider,event_id,url,match_day,resolved_at,valid_until
        FROM v6.fixture_link_registry WHERE fixture_id=$1 AND match_day=$6 AND starting_at=$2
        AND home_team_id=$3 AND away_team_id=$4 AND league_id=$5
        AND status='matched' AND valid_until>now()''',fixture_id,kickoff,home_id,away_id,league_id,
        kickoff.astimezone(ZoneInfo('America/Lima')).date(),timeout=0.1)
    return {r['provider']:{'event_id':r['event_id'],'url':r['url'],'match_day':r['match_day'].isoformat(),
            'resolved_at':r['resolved_at'].isoformat(),'valid_until':r['valid_until'].isoformat()}
            for r in rows if valid_event_url(r['provider'],r['url'],r['event_id'])}
