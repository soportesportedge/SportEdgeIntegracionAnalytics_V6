"""Send one explicitly marked link test to the existing authorized V6 test chat.

Reads only the V6 cache. Does not create predictions, signals, or outbox rows.
Repeated executions return the saved result instead of sending another message.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import ssl
import sys
from time import perf_counter
from zoneinfo import ZoneInfo

import asyncpg
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from integration.database import init_connection
from integration.fixture_links import cached_links, with_sportedge_link
from integration.telegram import request_body


async def run(fixture_id, send):
    now = datetime.now(timezone.utc)
    output = ROOT / 'docs' / ('fixture_link_telegram_test_' + now.strftime('%Y%m%d') + '.json')
    if send and output.exists():
        previous = json.loads(output.read_text(encoding='utf-8'))
        if previous.get('delivery_status') in ('attempted', 'uncertain', 'confirmed'):
            print(json.dumps({'delivery_status': previous['delivery_status'],
                              'message_id': previous.get('message_id'),
                              'audit_file': str(output), 'resent': False}))
            return

    chat = (ROOT / 'secrets/telegram_chat_id.txt').read_text().strip()
    deployment = json.loads((ROOT / 'docs/precision15_deployment_validation.json').read_text())
    assert chat == str(deployment['telegram_chat_id']), 'Configured test chat changed'
    conn = await asyncpg.connect('postgresql://v6@127.0.0.1:15436/sportedge_v6',
        password=(ROOT / 'secrets/db_password.txt').read_text().strip(), command_timeout=3)
    await init_connection(conn)
    try:
        async with conn.transaction(readonly=True):
            row = await conn.fetchrow('''SELECT fixture_id,starting_at,home_team_id,
                away_team_id,league_id,evidence FROM v6.fixture_link_registry
                WHERE fixture_id=$1 AND provider='365scores' AND status='matched'
                AND valid_until>now() ORDER BY match_day DESC LIMIT 1''', fixture_id)
            assert row, 'Fixture absent from valid cache'
            snapshot = {'id': row['fixture_id'], 'starting_at': row['starting_at'].isoformat(),
                **{k: row[k] for k in ('home_team_id', 'away_team_id', 'league_id')}}
            names = row['evidence']['source_names']
            snapshot.update(home_team_name=names['home'], away_team_name=names['away'],
                            league_name=names['league'])
            started = perf_counter()
            links = with_sportedge_link(snapshot,await cached_links(conn, snapshot))
            lookup_ms = round((perf_counter() - started) * 1000, 3)
            assert set(links) == {'365scores', 'betano', 'sportedge'}, 'Three verified links required'
    finally:
        await conn.close()

    # Reuse the production formatter's validated anchors, without fabricating a
    # live snapshot, probability, or possible-goal alert for this scheduled game.
    method, formatted = request_body({'snapshot': snapshot, 'prediction': {},
                                     'fixture_links': links}, chat)
    assert method == 'sendMessage'
    anchors = [p for p in formatted['text'].split('\n') if p.startswith('<a href=')]
    assert len(anchors) == 3, 'Production formatter omitted a link'
    kickoff = row['starting_at'].astimezone(ZoneInfo('America/Lima'))
    message = '\n'.join(['<b>🧪 PRUEBA DE ENLACES · SPORTEDGE V6</b>', '',
        f"⚽ <b>{html.escape(names['home'])} — {html.escape(names['away'])}</b>",
        f"🏆 {html.escape(names['league'])}",
        f"📅 {kickoff:%d/%m/%Y · %H:%M} (Perú)", '', *anchors, '',
        'Prueba técnica: no es una señal de apuesta.'])
    audit = {'checked_at': now.isoformat(), 'fixture_id': fixture_id,
        'snapshot_identity': snapshot, 'links': links, 'cache_lookup_ms': lookup_ms,
        'provider_network_calls_during_alert': 0, 'database_access': 'read_only_v6',
        'model_inference_created': False, 'outbox_row_created': False,
        'production_link_formatter_used': True, 'message_html': message,
        'authorized_configured_test_chat_verified': True,
        'delivery_status': 'preview', 'application_handoff_verified': False}

    if send:
        token = (ROOT / 'secrets/telegram_bot_token.txt').read_text().strip()
        url = 'https://api.telegram.org/bot' + token
        async with httpx.AsyncClient(verify=ssl.create_default_context(),
                                     trust_env=False, timeout=15) as client:
            response = await client.post(url + '/getChat', json={'chat_id': chat})
            body = response.json()
            assert response.status_code == 200 and body.get('ok') is True, 'Test chat unavailable'
            audit['chat_type'] = body['result']['type']
            audit['delivery_status'] = 'attempted'
            output.write_text(json.dumps(audit, indent=2, ensure_ascii=False), encoding='utf-8')
            try:
                started = perf_counter()
                response = await client.post(url + '/sendMessage', json={
                    'chat_id': chat, 'text': message, 'parse_mode': 'HTML',
                    'link_preview_options': {'is_disabled': True}})
                body = response.json()
                audit.update(http_status=response.status_code,
                             telegram_response_ms=round((perf_counter() - started) * 1000, 3))
                result = body.get('result', {})
                confirmed = (response.status_code == 200 and body.get('ok') is True
                             and isinstance(result.get('message_id'), int))
                audit['delivery_status'] = 'confirmed' if confirmed else 'uncertain'
                audit['message_id'] = result.get('message_id')
                actual = [e['url'] for e in result.get('entities', []) if e.get('type') == 'text_link']
                audit['telegram_returned_link_urls'] = actual
                audit['both_links_confirmed_by_telegram'] = (
                    set(actual) == {link['url'] for link in links.values()})
                audit['telegram_message_date'] = result.get('date')
            except (httpx.HTTPError, ValueError):
                audit['delivery_status'] = 'uncertain'
                audit['error'] = 'telegram_response_unconfirmed'
            output.write_text(json.dumps(audit, indent=2, ensure_ascii=False), encoding='utf-8')
        assert audit['delivery_status'] == 'confirmed', 'Delivery unconfirmed; do not resend automatically'
        assert audit['both_links_confirmed_by_telegram'], 'Telegram link entities differ'
    print(json.dumps({k: audit.get(k) for k in ('fixture_id', 'delivery_status',
        'message_id', 'cache_lookup_ms', 'both_links_confirmed_by_telegram')}, ensure_ascii=False))
    if send:
        print('Audit: ' + str(output))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixture-id', type=int, default=19741185)
    parser.add_argument('--send', action='store_true')
    args = parser.parse_args()
    try:
        asyncio.run(run(args.fixture_id, args.send))
    except httpx.HTTPError as exc:
        # HTTP exception messages can contain the bot token embedded in the URL.
        raise SystemExit('Telegram connection failed: ' + type(exc).__name__) from None
