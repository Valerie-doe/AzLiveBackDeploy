import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from django.conf import settings
from django.db import close_old_connections

from .jp_capture import (
    normalize_tiktok_username,
    process_social_comment,
    resolve_active_live,
    resolve_vendeur_from_tiktok_username,
)
from .models import Live

logger = logging.getLogger(__name__)

TIKTOOL_WS_BASE = 'wss://api.tik.tools'
TIKTOOL_CHECK_ALIVE_URL = 'https://api.tik.tools/webcast/check_alive'

_listeners: dict[int, '_TikToolLiveListener'] = {}
_listeners_lock = threading.Lock()


def tiktool_configured() -> bool:
    return bool(getattr(settings, 'TIKTOOL_API_KEY', ''))


def check_streamer_is_live(unique_id: str) -> bool | None:
    """Retourne True/False si TikTools répond, None si non configuré ou erreur réseau."""
    if not tiktool_configured():
        return None

    params = urllib.parse.urlencode(
        {
            'apiKey': settings.TIKTOOL_API_KEY,
            'unique_id': normalize_tiktok_username(unique_id),
        }
    )
    request = urllib.request.Request(
        f'{TIKTOOL_CHECK_ALIVE_URL}?{params}',
        headers={'User-Agent': 'AZLive/1.0'},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode('utf-8'))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError) as exc:
        logger.warning('TikTools check_alive failed for %s: %s', unique_id, exc)
        return None

    if isinstance(payload, dict):
        if 'is_live' in payload:
            return bool(payload['is_live'])
        if 'data' in payload and isinstance(payload['data'], dict):
            return bool(payload['data'].get('is_live') or payload['data'].get('alive'))
        return bool(payload.get('alive') or payload.get('live'))
    return False


def build_tiktok_diffusion(live: Live) -> dict[str, Any] | None:
    username = live.vendeur.tiktok_username
    if not username:
        return None

    unique_id = normalize_tiktok_username(username)
    is_live = check_streamer_is_live(unique_id)

    return {
        'username': username,
        'unique_id': unique_id,
        'status': 'LIVE' if is_live else 'PENDING_MANUAL',
        'is_live_on_tiktok': is_live,
        'tiktool_listener': tiktool_configured(),
        'demo': False,
        'instructions': (
            'Lancez le live sur TikTok (app ou Live Center). '
            'Les commentaires JP seront capturés automatiquement via TikTools.'
        ),
    }


def process_tiktool_chat_event(streamer_unique_id: str, event_data: dict[str, Any]) -> dict[str, Any]:
    user = event_data.get('user') or {}
    sender_id = str(user.get('uniqueId') or user.get('userId') or user.get('id') or '')
    sender_name = user.get('nickname') or user.get('uniqueId') or 'Client TikTok'
    comment_text = event_data.get('comment') or event_data.get('text') or ''

    vendeur = resolve_vendeur_from_tiktok_username(streamer_unique_id)
    live = resolve_active_live(vendeur) if vendeur else None

    return process_social_comment(
        sender_id=sender_id,
        sender_name=sender_name,
        comment_text=comment_text,
        channel='TikTok',
        vendeur=vendeur,
        live=live,
        id_field='tiktok_id',
    )


def _build_ws_url(unique_id: str) -> str:
    params = urllib.parse.urlencode(
        {
            'uniqueId': normalize_tiktok_username(unique_id),
            'apiKey': settings.TIKTOOL_API_KEY,
        }
    )
    return f'{TIKTOOL_WS_BASE}?{params}'


class _TikToolLiveListener(threading.Thread):
    daemon = True

    def __init__(self, live_id: int, unique_id: str, stop_event: threading.Event):
        super().__init__(name=f'tiktool-live-{live_id}')
        self.live_id = live_id
        self.unique_id = normalize_tiktok_username(unique_id)
        self.stop_event = stop_event

    def run(self):
        try:
            import websocket
        except ImportError:
            logger.error('websocket-client non installé: pip install websocket-client')
            return

        while not self.stop_event.is_set():
            ws_app = websocket.WebSocketApp(
                _build_ws_url(self.unique_id),
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            ws_app.run_forever(ping_interval=30, ping_timeout=10)
            if self.stop_event.wait(3):
                break

    def _on_message(self, _ws, message: str):
        close_old_connections()
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            return

        if payload.get('event') != 'chat':
            return

        event_data = payload.get('data') or {}
        try:
            result = process_tiktool_chat_event(self.unique_id, event_data)
            if result.get('status') != 'ignored':
                logger.info(
                    'JP TikTok capturé (live #%s, streamer @%s): %s',
                    self.live_id,
                    self.unique_id,
                    result.get('status'),
                )
        except Exception as exc:
            logger.warning('Erreur capture JP TikTok (live #%s): %s', self.live_id, exc)

    def _on_error(self, _ws, error):
        logger.warning('TikTools WebSocket error (live #%s): %s', self.live_id, error)

    def _on_close(self, _ws, close_status_code, close_msg):
        logger.info(
            'TikTools WebSocket fermé (live #%s): %s %s',
            self.live_id,
            close_status_code,
            close_msg,
        )


def start_tiktool_listener(live: Live) -> bool:
    if not tiktool_configured() or live.vendeur.is_demo_mode:
        return False

    username = live.vendeur.tiktok_username
    if not username:
        return False

    unique_id = normalize_tiktok_username(username)
    stop_event = threading.Event()

    with _listeners_lock:
        stop_tiktool_listener(live, lock_held=True)
        listener = _TikToolLiveListener(live.pk, unique_id, stop_event)
        _listeners[live.pk] = listener
        listener.start()

    logger.info('TikTools listener démarré pour live #%s (@%s)', live.pk, unique_id)
    return True


def stop_tiktool_listener(live: Live, lock_held: bool = False) -> bool:
    live_id = live.pk

    def _stop():
        listener = _listeners.pop(live_id, None)
        if not listener:
            return False
        listener.stop_event.set()
        return True

    if lock_held:
        return _stop()

    with _listeners_lock:
        return _stop()


def listener_status(live_id: int) -> dict[str, Any]:
    with _listeners_lock:
        listener = _listeners.get(live_id)
        if not listener:
            return {'running': False}
        return {
            'running': listener.is_alive(),
            'unique_id': listener.unique_id,
            'thread': listener.name,
        }
