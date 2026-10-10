"""Create new local V6 credentials. Existing files are never overwritten."""
from pathlib import Path
import secrets
root=Path(__file__).resolve().parents[1]/'secrets'
root.mkdir(exist_ok=True)
for name in ['db_password','model_api_key','integration_api_key']:
    p=root/(name+'.txt')
    if not p.exists():p.write_text(secrets.token_urlsafe(32),encoding='utf-8')
for name in ['telegram_bot_token','telegram_chat_id','source_database_url']:
    p=root/(name+'.txt')
    if not p.exists():p.write_text('',encoding='utf-8')
print('Local V6 secret files ready; credentials were not printed.')
