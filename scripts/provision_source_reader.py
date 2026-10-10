"""Add a dedicated V6 SELECT-only role; never alter an existing application role."""
import argparse
from pathlib import Path
import secrets
import subprocess
from urllib.parse import quote

parser=argparse.ArgumentParser()
parser.add_argument('--container',default='sportedge_v2_db')
parser.add_argument('--database',default='sportedge_v2')
parser.add_argument('--owner',default='sportedge')
parser.add_argument('--host',default='host.docker.internal')
parser.add_argument('--port',type=int,default=5401)
args=parser.parse_args()
for value in [args.database,args.owner]:
    if not value.replace('_','').isalnum():raise SystemExit('Use simple PostgreSQL identifiers')
root=Path(__file__).resolve().parents[1]/'secrets'
private=root/'source_password.txt'
if not private.exists():private.write_text(secrets.token_urlsafe(32),encoding='utf-8')
password=private.read_text().strip()
if not password.replace('_','').replace('-','').isalnum():raise SystemExit('Invalid generated credential')
sql=f"""DO $$ BEGIN
 IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='sportedge_v6_ro') THEN
   CREATE ROLE sportedge_v6_ro LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT PASSWORD '{password}' CONNECTION LIMIT 2;
 END IF;
END $$;
ALTER ROLE sportedge_v6_ro SET default_transaction_read_only=on;
ALTER ROLE sportedge_v6_ro SET statement_timeout='1500ms';
ALTER ROLE sportedge_v6_ro SET lock_timeout='100ms';
ALTER ROLE sportedge_v6_ro SET jit=off;
ALTER ROLE sportedge_v6_ro SET idle_in_transaction_session_timeout='5000ms';
GRANT CONNECT ON DATABASE {args.database} TO sportedge_v6_ro;
GRANT USAGE ON SCHEMA public TO sportedge_v6_ro;
GRANT SELECT ON match_centre_snapshots,fixtures,leagues,teams,head_to_head_snapshots,standings_snapshots,odds_snapshots TO sportedge_v6_ro;
"""
result=subprocess.run(['docker','exec','-i',args.container,'psql','-U',args.owner,'-d',args.database,'-v','ON_ERROR_STOP=1','-q'],input=sql,text=True,capture_output=True)
if result.returncode:raise SystemExit('Could not provision the dedicated V6 reader (database error details suppressed to protect credentials).')
dsn=f'postgresql://sportedge_v6_ro:{quote(password)}@{args.host}:{args.port}/{args.database}'
(root/'source_database_url.txt').write_text(dsn,encoding='utf-8')
print('Dedicated SELECT-only V6 reader provisioned; existing application roles and tables were not changed.')
