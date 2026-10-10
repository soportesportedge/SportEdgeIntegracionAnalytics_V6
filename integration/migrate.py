import asyncio
import hashlib
from integration.config import ROOT
from integration.database import pool, transaction

async def run():
    p=await pool()
    try:
        async with transaction(p) as c:
            await c.execute('SELECT pg_advisory_xact_lock(610100)')
            await c.execute('CREATE SCHEMA IF NOT EXISTS v6; CREATE TABLE IF NOT EXISTS v6.schema_migrations(version text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())')
            for file in sorted((ROOT/'sql').glob('[0-9][0-9][0-9]_*.sql')):
                script=file.read_text();digest=hashlib.sha256(script.encode()).hexdigest();version=file.stem.split('_')[0]
                prior=await c.fetchval('SELECT sha256 FROM v6.schema_migrations WHERE version=$1',version)
                if prior and prior!=digest: raise RuntimeError('Applied migration checksum changed: '+version)
                if not prior:
                    await c.execute(script)
                    await c.execute('INSERT INTO v6.schema_migrations(version,sha256) VALUES ($1,$2)',version,digest)
        print('V6 migration complete')
    finally: await p.close()

if __name__=='__main__':asyncio.run(run())
