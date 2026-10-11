"""Shared schedule for the independent V6 daily fixture catalogue module."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from integration.config import bounded

COMPONENT = 'v6.fixture_catalogues'
TIMEZONE = 'America/Lima'
SERVICES = ('links-worker', 'betano-web')


def configuration(now=None):
    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(TIMEZONE))
    hour = int(bounded('V6_LINK_BUILD_HOUR', 2, 0, 23))
    minute = int(bounded('V6_LINK_BUILD_MINUTE', 0, 0, 59))
    start = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    next_start = start if local < start else start + timedelta(days=1)
    return {'component': COMPONENT, 'name': 'Catálogos diarios de partidos V6',
        'timezone': TIMEZONE, 'daily_start': f'{hour:02d}:{minute:02d}',
        'next_daily_start_at': next_start.isoformat(), 'local_match_day': local.date().isoformat(),
        'refresh_seconds': int(bounded('V6_LINK_REFRESH_SECONDS', 21600, 900, 86400)),
        'tick_seconds': 60, 'startup_catch_up': True, 'services': list(SERVICES),
        'alert_lookup': 'local_v6_cache_only', 'execution': 'independent_docker_services'}


def due_day(now=None):
    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(TIMEZONE))
    hour = int(bounded('V6_LINK_BUILD_HOUR', 2, 0, 23))
    minute = int(bounded('V6_LINK_BUILD_MINUTE', 0, 0, 59))
    return local.date() if (local.hour, local.minute) >= (hour, minute) else None
