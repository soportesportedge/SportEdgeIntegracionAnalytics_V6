from contextlib import asynccontextmanager
import json
import asyncpg
from integration.config import database_url, secret, bounded

async def init_connection(conn):
    await conn.set_type_codec('jsonb',schema='pg_catalog',encoder=json.dumps,decoder=json.loads,format='text')

async def pool():
    return await asyncpg.create_pool(database_url(),password=secret('V6_DB_PASSWORD') or None,
        min_size=1,max_size=int(bounded('V6_DB_POOL_SIZE',2,1,4)),command_timeout=5,
        server_settings={'application_name':'sportedge_v6','statement_timeout':'3000',
                         'lock_timeout':'500','idle_in_transaction_session_timeout':'5000'},
        init=init_connection)

@asynccontextmanager
async def transaction(p):
    async with p.acquire() as conn, conn.transaction(): yield conn
