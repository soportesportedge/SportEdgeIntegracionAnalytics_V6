"""Read-only verification of the dedicated V6 source role; never output its DSN."""
import argparse
import asyncio
from datetime import datetime,timezone
import json
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit
import asyncpg

ROOT=Path(__file__).resolve().parents[1]
TABLES={'match_centre_snapshots','fixtures','leagues','teams','head_to_head_snapshots','standings_snapshots','odds_snapshots'}
parser=argparse.ArgumentParser()
parser.add_argument('--host',help='Optional host override for running verification outside Docker.')
args=parser.parse_args()

async def run():
    dsn=(ROOT/'secrets'/'source_database_url.txt').read_text().strip()
    if args.host:
        parsed=urlsplit(dsn)
        auth=parsed.netloc.rsplit('@',1)[0]
        dsn=urlunsplit((parsed.scheme,auth+'@'+args.host+':'+str(parsed.port or 5432),parsed.path,parsed.query,parsed.fragment))
    conn=await asyncpg.connect(dsn,command_timeout=3,server_settings={'application_name':'sportedge_v6_permission_verification','default_transaction_read_only':'on'})
    try:
        async with conn.transaction(readonly=True):
            row=await conn.fetchrow('SELECT rolname,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolinherit,rolconnlimit FROM pg_roles WHERE rolname=current_user')
            permissions=await conn.fetch("""SELECT c.relname,
                has_table_privilege(c.oid,'SELECT') AS can_select,
                has_table_privilege(c.oid,'INSERT') AS can_insert,
                has_table_privilege(c.oid,'UPDATE') AS can_update,
                has_table_privilege(c.oid,'DELETE') AS can_delete,
                has_table_privilege(c.oid,'TRUNCATE') AS can_truncate,
                has_table_privilege(c.oid,'TRIGGER') AS can_trigger,
                has_table_privilege(c.oid,'REFERENCES') AS can_references
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='public' AND c.relkind IN ('r','p') ORDER BY c.relname""")
            settings={n:await conn.fetchval('SELECT current_setting($1)',n) for n in ['default_transaction_read_only','statement_timeout','lock_timeout','idle_in_transaction_session_timeout','jit']}
            collectors=await conn.fetchval("SELECT count(*) FROM pg_stat_activity WHERE usename=current_user AND application_name='sportedge_v6_readonly'")
            schema_create=await conn.fetchval("SELECT has_schema_privilege('public','CREATE')")
            readable={r['relname'] for r in permissions if r['can_select']}
            checks={
                'dedicated_role':row['rolname']=='sportedge_v6_ro',
                'no_administrative_flags':not any(row[k] for k in ['rolsuper','rolcreatedb','rolcreaterole','rolreplication','rolinherit']),
                'connection_limit_two':row['rolconnlimit']==2,
                'select_exactly_seven_source_tables':readable==TABLES,
                'no_table_write_privileges':not any(r[k] for r in permissions for k in ['can_insert','can_update','can_delete','can_truncate','can_trigger','can_references']),
                'no_source_schema_create':not schema_create,
                'read_only_default':settings['default_transaction_read_only']=='on',
                'one_running_collector_source_session':collectors==1,
            }
            report={'checked_at':datetime.now(timezone.utc).isoformat(),'checks':checks,'role':dict(row),'settings':settings,
                    'source_table_permissions':[dict(r) for r in permissions if r['relname'] in TABLES],
                    'collector_source_sessions':collectors,'passed':all(checks.values())}
    finally:await conn.close()
    (ROOT/'docs'/'source_reader_validation.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))
    assert report['passed'],'Dedicated source role verification failed'

asyncio.run(run())
