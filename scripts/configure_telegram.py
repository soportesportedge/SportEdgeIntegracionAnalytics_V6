"""Copy only the authorized Telegram token to the NEW ignored secret file."""
import argparse
from pathlib import Path
parser=argparse.ArgumentParser()
parser.add_argument('--source-env',required=True)
parser.add_argument('--chat-id',required=True)
args=parser.parse_args()
token=None
for line in Path(args.source_env).read_text(encoding='utf-8-sig').splitlines():
    if line.strip().startswith('TELEGRAM_BOT_TOKEN='):
        token=line.split('=',1)[1].strip().strip('\"\'')
if not token:raise SystemExit('No configured Telegram bot token in supplied environment file')
root=Path(__file__).resolve().parents[1]/'secrets'
(root/'telegram_bot_token.txt').write_text(token,encoding='utf-8')
(root/'telegram_chat_id.txt').write_text(args.chat_id,encoding='utf-8')
print('Telegram credentials configured for the separate V6 chat. Token was not printed.')
