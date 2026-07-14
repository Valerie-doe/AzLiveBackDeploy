"""Envoi de messages dans le chat public d'un live TikTok via TikTools.

Nécessite TIKTOOL_API_KEY et TIKTOK_SESSION_COOKIES (session du compte streamer).
Voir https://tik.tools/docs — endpoint POST /chat-send
"""
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

TIKTOOL_CHAT_SEND_URL = 'https://api.tik.tools/chat-send'


def _tiktool_configured() -> bool:
    return bool(getattr(settings, 'TIKTOOL_API_KEY', ''))


def _normalize_channel(channel: str | None) -> str:
    return (channel or '').lstrip('@').strip().lower()


def tiktok_chat_send_configured() -> bool:
    return bool(_tiktool_configured() and getattr(settings, 'TIKTOK_SESSION_COOKIES', ''))


def send_tiktok_live_chat_message(channel: str, text: str) -> dict[str, Any]:
    """Publie un message dans le chat du live (compte streamer connecté via cookies)."""
    if not _tiktool_configured():
        return {'sent': False, 'error': 'TIKTOOL_API_KEY manquant.', 'channel': 'TikTok', 'via': 'live_chat'}
    cookies = getattr(settings, 'TIKTOK_SESSION_COOKIES', '')
    if not cookies:
        return {
            'sent': False,
            'error': 'TIKTOK_SESSION_COOKIES manquant (session TikTok du streamer).',
            'channel': 'TikTok',
            'via': 'live_chat',
        }

    unique_id = _normalize_channel(channel)
    if not unique_id:
        return {'sent': False, 'error': 'Identifiant TikTok streamer invalide.', 'channel': 'TikTok', 'via': 'live_chat'}

    params = urllib.parse.urlencode({'apiKey': settings.TIKTOOL_API_KEY})
    body = json.dumps({'channel': unique_id, 'text': text}).encode('utf-8')
    request = urllib.request.Request(
        f'{TIKTOOL_CHAT_SEND_URL}?{params}',
        data=body,
        method='POST',
        headers={
            'Content-Type': 'application/json',
            'x-cookie-header': cookies,
            'User-Agent': 'AZLive/1.0',
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')
        logger.warning('TikTools chat-send HTTP %s (@%s): %s', exc.code, unique_id, detail[:200])
        return {'sent': False, 'error': detail or str(exc), 'channel': 'TikTok', 'via': 'live_chat'}
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        logger.warning('TikTools chat-send failed (@%s): %s', unique_id, exc)
        return {'sent': False, 'error': str(exc), 'channel': 'TikTok', 'via': 'live_chat'}

    delivered = bool(payload.get('delivered'))
    if not delivered:
        logger.warning('TikTok chat non délivré (@%s): %s', unique_id, payload)
    return {
        'sent': delivered,
        'delivered': delivered,
        'channel': 'TikTok',
        'via': 'live_chat',
        'msg_id': payload.get('msg_id'),
        'detail': payload.get('error') or payload.get('message'),
    }
