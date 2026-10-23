"""Shared schedule for the independent V6 daily fixture catalogue module."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from integration.config import bounded

COMPONENT = 'v6.fixture_catalogues'
TIMEZONE = 'America/Lima'
SERVICES = ('links-worker', 'betano-web')


def refresh_seconds():
    return int(bounded('V6_LINK_REFRESH_SECONDS', 7200, 900, 86400))


def daily_start_at(day):
    return datetime(day.year,day.month,day.day,int(bounded('V6_LINK_BUILD_HOUR',2,0,23)),
                    int(bounded('V6_LINK_BUILD_MINUTE',0,0,59)),tzinfo=ZoneInfo(TIMEZONE)).astimezone(timezone.utc)


def slot_at(now=None):
    """Fixed slots, anchored at the daily start, including the midnight slot."""
    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(TIMEZONE))
    start = local.replace(hour=int(bounded('V6_LINK_BUILD_HOUR', 2, 0, 23)),
                          minute=int(bounded('V6_LINK_BUILD_MINUTE', 0, 0, 59)), second=0, microsecond=0)
    if local < start:
        start -= timedelta(days=1)
    return (start + timedelta(seconds=int((local-start).total_seconds()) // refresh_seconds()
                             * refresh_seconds())).astimezone(timezone.utc)


def next_slot(now=None):
    return slot_at(now) + timedelta(seconds=refresh_seconds())


def incremental_window(now=None):
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return now-timedelta(hours=1), now+timedelta(hours=2)


def scheduled_days(now=None):
    now = now or datetime.now(timezone.utc)
    start, end = incremental_window(now)
    zone = ZoneInfo(TIMEZONE)
    days = {start.astimezone(zone).date(), end.astimezone(zone).date()}
    if due_day(now):
        days.add(due_day(now))
    return sorted(days)


def configuration(now=None):
    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(TIMEZONE))
    hour = int(bounded('V6_LINK_BUILD_HOUR', 2, 0, 23))
    minute = int(bounded('V6_LINK_BUILD_MINUTE', 0, 0, 59))
    start = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    next_start = start if local < start else start + timedelta(days=1)
    return {'component': COMPONENT, 'name': 'Catálogos diarios de partidos V6',
        'timezone': TIMEZONE, 'daily_start': f'{hour:02d}:{minute:02d}',
        'next_daily_start_at': next_start.isoformat(), 'local_match_day': local.date().isoformat(),
        'refresh_seconds': refresh_seconds(), 'next_refresh_at':next_slot(now).astimezone(ZoneInfo(TIMEZONE)).isoformat(),
        'daily_mode':'full_day', 'refresh_mode':'unresolved_only',
        'incremental_past_hours':1, 'incremental_future_hours':2,
        'tick_seconds': 60, 'startup_catch_up': True, 'services': list(SERVICES),
        'alert_lookup': 'local_v6_cache_only', 'execution': 'independent_docker_services'}


def due_day(now=None):
    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(TIMEZONE))
    hour = int(bounded('V6_LINK_BUILD_HOUR', 2, 0, 23))
    minute = int(bounded('V6_LINK_BUILD_MINUTE', 0, 0, 59))
    return local.date() if (local.hour, local.minute) >= (hour, minute) else None
