import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from .jp_capture import (
    normalize_tiktok_username,
    process_social_comment,
    resolve_active_live,
    resolve_vendeur_from_tiktok_username,
)
from .models import Live, Vendeur

logger = logging.getLogger(__name__)

TIKTOOL_WS_BASE = 'wss://api.tik.tools'
TIKTOOL_CHECK_ALIVE_URL = 'https://api.tik.tools/webcast/check_alive'
TIKTOOL_LIVE_STATUS_URL = 'https://api.tik.tools/webcast/live_status'
TIKTOOL_ROOM_ID_URL = 'https://api.tik.tools/webcast/room_id'
TIKTOOL_ROOM_INFO_URL = 'https://api.tik.tools/webcast/room_info'

# Codes TikTools = fin de live explicite (doc v3.2).
_WS_STREAM_END_CODES = {4005, 4006, 4555, 4404}

# Signaux FORTS : prouvent un live réel (chat, dons, démarrage explicite…).
_STRONG_LIVE_EVENTS = frozenset({
    'chat',
    'gift',
    'member',
    'like',
    'social',
    'follow',
    'share',
    'subscribe',
    'streamStart',
    'stream_start',
    'liveStart',
    'live_start',
})

# Signaux FAIBLES : handshake TikTools (souvent envoyés hors live / room stale).
# Ne doivent JAMAIS créer un Live AZLive sans confirmation REST room_id.
_WEAK_LIVE_EVENTS = frozenset({
    'roomUserSeq',
    'WebcastRoomUserSeqMessage',
})

# Rétrocompat : union utilisée pour « note activity » une fois le live déjà ouvert.
_LIVE_ACTIVITY_EVENTS = _STRONG_LIVE_EVENTS | _WEAK_LIVE_EVENTS

_listeners: dict[int, '_TikToolLiveListener'] = {}
_scouts: dict[str, '_TikToolLiveListener'] = {}
_listeners_lock = threading.Lock()
# File d'attente pool WS : lives qui veulent un slot quand le max est atteint.
# Chaque entrée = (live_id, unique_id). FIFO.
_ws_pending: list[tuple[int, str]] = []
_ws_pending_live_ids: set[int] = set()
_last_tiktok_sync_at: datetime | None = None
_tiktok_sync_lock = threading.Lock()
_last_vendeur_sync_at: dict[int, datetime] = {}
_rate_limited_until: datetime | None = None
_rate_limit_lock = threading.Lock()
_ws_rate_limited_until: datetime | None = None
_ws_rate_limit_lock = threading.Lock()


def tiktool_configured() -> bool:
    return bool(getattr(settings, 'TIKTOOL_API_KEY', ''))


def _mark_rate_limited(seconds: float = 90.0) -> None:
    """Pause après un 429 TikTools (sandbox = 20 req/min)."""
    global _rate_limited_until
    until = timezone.now() + timedelta(seconds=max(seconds, 60.0))
    with _rate_limit_lock:
        if _rate_limited_until is None or until > _rate_limited_until:
            _rate_limited_until = until
            logger.warning(
                'TikTools rate-limit 429 : pause API jusqu’à %s',
                _rate_limited_until.isoformat(),
            )


def _tiktool_is_rate_limited() -> bool:
    with _rate_limit_lock:
        if _rate_limited_until is None:
            return False
        if timezone.now() >= _rate_limited_until:
            return False
        return True


def _ws_cooldown_seconds(default: float = 3600.0) -> float:
    """Durée de pause après un 4429 (configurable via TIKTOOL_WS_COOLDOWN_SECONDS)."""
    try:
        return max(float(getattr(settings, 'TIKTOOL_WS_COOLDOWN_SECONDS', default) or default), 300.0)
    except (TypeError, ValueError):
        return max(default, 300.0)


def _ws_mode() -> str:
    """always = scout permanent ; on_demand = WS seulement pendant un live (économie quota)."""
    mode = str(getattr(settings, 'TIKTOOL_WS_MODE', 'on_demand') or 'on_demand').strip().lower()
    if mode in {'always', 'permanent', 'scout'}:
        return 'always'
    return 'on_demand'


def _ws_on_demand() -> bool:
    return _ws_mode() == 'on_demand'


def _ws_manual_capture() -> bool:
    """Si True : pas d'ouverture WS auto — uniquement via « Activer capture JP »."""
    return bool(getattr(settings, 'TIKTOOL_WS_MANUAL_CAPTURE', True))


def _ws_max_connections() -> int:
    """Plafond de WebSockets TikTools ouverts en parallèle (pool)."""
    try:
        return max(1, int(getattr(settings, 'TIKTOOL_WS_MAX_CONNECTIONS', 2) or 2))
    except (TypeError, ValueError):
        return 2


def _ws_active_unique_ids_locked() -> set[str]:
    """Unique_ids ayant un thread WS vivant (scout ou listener)."""
    active: set[str] = set()
    for unique_id, scout in _scouts.items():
        if scout is not None and scout.is_alive() and not scout.stop_event.is_set():
            active.add(unique_id)
    for listener in _listeners.values():
        if listener is not None and listener.is_alive() and not listener.stop_event.is_set():
            active.add(listener.unique_id)
    return active


def _ws_active_count_locked() -> int:
    return len(_ws_active_unique_ids_locked())


def _ws_pool_has_slot_locked() -> bool:
    return _ws_active_count_locked() < _ws_max_connections()


def _ws_enqueue_pending_locked(live_id: int, unique_id: str) -> int:
    """Ajoute un live en file d'attente pool. Retourne la position 1-based."""
    if live_id in _ws_pending_live_ids:
        for idx, (lid, _) in enumerate(_ws_pending):
            if lid == live_id:
                return idx + 1
        return len(_ws_pending)
    _ws_pending.append((live_id, unique_id))
    _ws_pending_live_ids.add(live_id)
    return len(_ws_pending)


def _ws_remove_pending_locked(live_id: int) -> None:
    global _ws_pending
    if live_id not in _ws_pending_live_ids:
        return
    _ws_pending = [(lid, uid) for lid, uid in _ws_pending if lid != live_id]
    _ws_pending_live_ids.discard(live_id)


def _ws_promote_pending_locked() -> int:
    """Ouvre des WS pour les lives en file tant qu'il reste des slots.

    À appeler sous `_listeners_lock`, typiquement après libération d'un slot.
    """
    promoted = 0
    while _ws_pool_has_slot_locked() and _ws_pending:
        live_id, unique_id = _ws_pending.pop(0)
        _ws_pending_live_ids.discard(live_id)
        try:
            close_old_connections()
            live = (
                Live.objects.filter(pk=live_id, statut=Live.STATUT_EN_COURS)
                .select_related('vendeur')
                .first()
            )
        except Exception:
            logger.exception('Pool WS : impossible de recharger live #%s', live_id)
            continue
        if live is None:
            logger.info('Pool WS : live #%s plus en cours — retiré de la file', live_id)
            continue
        existing = _scouts.get(unique_id)
        if existing and existing.is_alive() and not existing.stop_event.is_set():
            existing.live_id = live_id
            _listeners[live_id] = existing
            promoted += 1
            continue
        _start_listener_locked(unique_id, live_id, scout=True)
        promoted += 1
        logger.info(
            'Pool WS : slot libre → démarrage live #%s (@%s) '
            '(actifs=%s/%s, file=%s)',
            live_id,
            unique_id,
            _ws_active_count_locked(),
            _ws_max_connections(),
            len(_ws_pending),
        )
    return promoted


def tiktool_ws_pool_status() -> dict[str, Any]:
    """État du pool WS (debug / monitoring)."""
    with _listeners_lock:
        active_ids = sorted(_ws_active_unique_ids_locked())
        pending = list(_ws_pending)
    return {
        'mode': _ws_mode(),
        'max_connections': _ws_max_connections(),
        'active': len(active_ids),
        'active_unique_ids': active_ids,
        'pending': len(pending),
        'pending_live_ids': [lid for lid, _ in pending],
        'ws_rate_limited': _tiktool_ws_is_rate_limited(),
        'ws_rate_limit_remaining_seconds': round(tiktool_ws_rate_limit_remaining_seconds(), 1),
    }


def _mark_ws_rate_limited(seconds: float | None = None) -> None:
    """Sandbox TikTools : quota WS (≈ 50–60 sessions / 24h ou /heure selon le plan).

    Après un close 4429 on coupe les nouvelles connexions WS pour ne pas brûler
    le quota en boucle. La détection continue via REST (voir recover_tiktool_listeners).
    """
    global _ws_rate_limited_until
    pause = _ws_cooldown_seconds() if seconds is None else max(float(seconds), 300.0)
    until = timezone.now() + timedelta(seconds=pause)
    with _ws_rate_limit_lock:
        if _ws_rate_limited_until is None or until > _ws_rate_limited_until:
            _ws_rate_limited_until = until
            logger.warning(
                'TikTools WebSocket quota (4429) : pause WS jusqu’à %s (%.0fs). '
                'Détection live basculée sur REST pendant ce temps '
                '(évite de reconnecter en boucle — chaque reconnexion consomme 1 session).',
                _ws_rate_limited_until.isoformat(),
                pause,
            )


def _tiktool_ws_is_rate_limited() -> bool:
    with _ws_rate_limit_lock:
        if _ws_rate_limited_until is None:
            return False
        if timezone.now() >= _ws_rate_limited_until:
            return False
        return True


def tiktool_ws_rate_limit_remaining_seconds() -> float:
    with _ws_rate_limit_lock:
        if _ws_rate_limited_until is None:
            return 0.0
        return max(0.0, (_ws_rate_limited_until - timezone.now()).total_seconds())


def _is_valid_unique_id(unique_id: str) -> bool:
    return bool(re.fullmatch(r'[a-z0-9._-]+', unique_id or ''))


def resolve_vendeur_tiktok_unique_id(vendeur: Vendeur) -> str | None:
    """Retourne le @TikTok utilisable pour détecter un live (compte connecté).

    Priorité :
    1. `tiktok_username` s'il est un unique_id valide (ex. azplus.mg)
    2. Dernier `unique_id` connu dans diffusion_plateformes d'un live de ce vendeur
    """
    candidate = normalize_tiktok_username(vendeur.tiktok_username)
    if _is_valid_unique_id(candidate):
        return candidate

    recent = (
        Live.objects.filter(vendeur=vendeur)
        .exclude(diffusion_plateformes__isnull=True)
        .order_by('-date_live', '-id')[:20]
    )
    for live in recent:
        tiktok = dict((live.diffusion_plateformes or {}).get('tiktok') or {})
        for key in ('unique_id', 'username'):
            found = normalize_tiktok_username(str(tiktok.get(key) or ''))
            if _is_valid_unique_id(found):
                # Répare le profil vendeur pour les prochains cycles.
                if vendeur.tiktok_username != found:
                    vendeur.tiktok_username = found
                    vendeur.save(update_fields=['tiktok_username'])
                    logger.info(
                        'Vendeur #%s : tiktok_username réparé → @%s (depuis live #%s)',
                        vendeur.pk,
                        found,
                        live.pk,
                    )
                return found
    return None


def iter_connected_tiktok_vendeurs(*, vendeur_id: int | None = None):
    """Vendeurs avec TikTok OAuth connecté + handle détectable."""
    qs = (
        Vendeur.objects.exclude(tiktok_open_id__isnull=True)
        .exclude(tiktok_open_id='')
        .exclude(is_demo_mode=True)
        .order_by('id')
    )
    if vendeur_id is not None:
        qs = qs.filter(pk=vendeur_id)
    for vendeur in qs:
        unique_id = resolve_vendeur_tiktok_unique_id(vendeur)
        if unique_id:
            yield vendeur, unique_id


def _tiktool_get(url: str, params: dict[str, str]) -> dict[str, Any] | None:
    if _tiktool_is_rate_limited():
        return None
    query = dict(params)
    query['apiKey'] = settings.TIKTOOL_API_KEY
    request = urllib.request.Request(
        f'{url}?{urllib.parse.urlencode(query)}',
        headers={'User-Agent': 'AZLive/1.0'},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode('utf-8', errors='replace'))
    except urllib.error.HTTPError as exc:
        body = ''
        try:
            body = exc.read().decode('utf-8', errors='replace')[:300]
        except Exception:
            pass
        if exc.code == 429:
            _mark_rate_limited(90.0)
            return None
        logger.warning(
            'TikTools GET %s failed: %s (params=%s body=%s)',
            url,
            exc,
            {k: v for k, v in params.items() if k != 'apiKey'},
            body or '-',
        )
        return None
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        logger.warning('TikTools GET %s failed: %s', url, exc)
        return None
    return payload if isinstance(payload, dict) else None


def _tiktool_post(url: str, body: dict[str, Any]) -> dict[str, Any] | None:
    if _tiktool_is_rate_limited():
        return None
    query = urllib.parse.urlencode({'apiKey': settings.TIKTOOL_API_KEY})
    request = urllib.request.Request(
        f'{url}?{query}',
        data=json.dumps(body).encode('utf-8'),
        method='POST',
        headers={
            'Content-Type': 'application/json',
            'User-Agent': 'AZLive/1.0',
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode('utf-8', errors='replace'))
    except urllib.error.HTTPError as exc:
        err_body = ''
        try:
            err_body = exc.read().decode('utf-8', errors='replace')[:300]
        except Exception:
            pass
        if exc.code == 429:
            _mark_rate_limited(90.0)
            return None
        logger.warning(
            'TikTools POST %s failed: %s (body_sent=%s resp=%s)',
            url,
            exc,
            {k: body.get(k) for k in ('unique_id', 'uniqueId', 'room_id') if k in body},
            err_body or '-',
        )
        return None
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        logger.warning('TikTools POST %s failed: %s', url, exc)
        return None
    return payload if isinstance(payload, dict) else None


def _request_check_alive(*, unique_id: str | None = None, room_id: str | None = None) -> dict[str, Any] | None:
    params: dict[str, str] = {}
    if room_id:
        params['room_id'] = str(room_id)
    elif unique_id:
        params['unique_id'] = normalize_tiktok_username(unique_id)
    else:
        return None
    return _tiktool_get(TIKTOOL_CHECK_ALIVE_URL, params)


def _extract_room_id(payload: dict[str, Any] | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get('data')
    if isinstance(data, dict) and data.get('room_id'):
        return str(data.get('room_id'))
    if payload.get('room_id'):
        return str(payload.get('room_id'))
    return None


def _check_live_via_live_status(unique_id: str) -> tuple[bool | None, str | None]:
    """GET /webcast/live_status — pré-check recommandé par TikTools (relay).

    Accepte uniqueId et unique_id (docs TikTools).
    """
    # Les deux noms de param : la doc utilise parfois uniqueId, parfois unique_id.
    payload = _tiktool_get(
        TIKTOOL_LIVE_STATUS_URL,
        {'uniqueId': unique_id, 'unique_id': unique_id},
    )
    room_id = _extract_room_id(payload)
    if not payload:
        return None, room_id
    data = payload.get('data')
    if isinstance(data, dict) and 'is_live' in data:
        return bool(data.get('is_live')), room_id or _extract_room_id({'data': data})
    return _parse_live_state(payload), room_id


def _check_live_via_room_id(unique_id: str) -> tuple[bool | None, str | None]:
    """POST /webcast/room_id — résolution serveur TikTools.

    Sur Sandbox (Railway / IP datacenter) cet endpoint échoue souvent en 404 :
    la résolution live est moins fiable hors plans Pro+. Dans ce cas on retourne
    None (indéterminé) pour laisser live_status décider.
    """
    payload = _tiktool_post(
        TIKTOOL_ROOM_ID_URL,
        {'unique_id': unique_id, 'uniqueId': unique_id},
    )
    room_id = _extract_room_id(payload)
    if not payload:
        return None, room_id
    data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
    if data.get('alive') is True:
        return True, room_id
    state = _parse_live_state(payload)
    if state is True:
        return True, room_id
    if data.get('alive') is False and not data.get('cached', True):
        return False, room_id
    return None, room_id


def _check_live_via_room_info(unique_id: str) -> tuple[bool | None, str | None]:
    """POST /webcast/room_info — sans page TikTok (évite le WAF)."""
    payload = _tiktool_post(TIKTOOL_ROOM_INFO_URL, {'unique_id': unique_id})
    room_id = _extract_room_id(payload)
    if not payload:
        return None, room_id
    data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
    if data.get('alive') is True:
        return True, room_id
    state = _parse_live_state(payload)
    if state is True:
        return True, room_id
    return None, room_id


def _parse_live_state(payload: dict[str, Any]) -> bool | None:
    if 'is_live' in payload:
        return bool(payload['is_live'])
    if 'alive' in payload:
        return bool(payload['alive'])
    if 'data' in payload and isinstance(payload['data'], dict):
        data = payload['data']
        if 'is_live' in data:
            return bool(data.get('is_live'))
        if 'alive' in data:
            return bool(data.get('alive'))
        if data.get('live') is not None:
            return bool(data.get('live'))
    if 'live' in payload:
        return bool(payload.get('live'))
    return None


def _resolve_signed_live_state(payload: dict[str, Any]) -> bool | None:
    signed_url = str(payload.get('signed_url') or '')
    if not signed_url:
        return None
    headers = payload.get('headers') or {'User-Agent': 'AZLive/1.0'}
    request = urllib.request.Request(signed_url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            resolved = json.loads(response.read().decode('utf-8', errors='replace'))
    except Exception as exc:  # noqa: BLE001
        logger.warning('TikTools signed check fetch failed: %s', exc)
        return None

    if isinstance(resolved, dict):
        data = resolved.get('data')
        if isinstance(data, list) and data:
            alive = data[0].get('alive')
            if alive is not None:
                return bool(alive)
        return _parse_live_state(resolved)
    return None


def _check_alive_for_room(room_id: str) -> bool | None:
    """Vérifie définitivement un room_id via /webcast/check_alive (+ signed_url)."""
    payload = _request_check_alive(room_id=str(room_id))
    if not payload:
        return None
    state = _parse_live_state(payload)
    if state is not None:
        return state
    return _resolve_signed_live_state(payload)


def _detect_live_status(unique_id: str) -> tuple[bool | None, str | None]:
    """Détection live robuste (ordre TikTools recommandé).

    1) live_status (rapide, adapté serveur)
    2) room_id (peut 404 en Sandbox depuis Railway)
    3) check_alive en dernier recours
    """
    normalized = normalize_tiktok_username(unique_id)

    status_hint, room_id = _check_live_via_live_status(normalized)
    if status_hint is True:
        logger.info('TikTok @%s : live_status=True (room=%s)', normalized, room_id)
        return True, room_id
    if status_hint is False:
        # Cache peut être stale → tenter room_id une fois.
        room_hint, room_id2 = _check_live_via_room_id(normalized)
        if room_hint is True:
            return True, room_id2 or room_id
        if room_hint is False:
            return False, room_id2 or room_id
        logger.info(
            'TikTok @%s : live_status=False, room_id indéterminé — statut=None',
            normalized,
        )
        return None, room_id

    # live_status failed/absent → room_id
    room_hint, room_id = _check_live_via_room_id(normalized)
    if room_hint is not None:
        return room_hint, room_id

    # Dernier recours
    if room_id:
        alive = _check_alive_for_room(room_id)
        if alive is not None:
            return alive, room_id
    payload = _request_check_alive(unique_id=normalized)
    if payload:
        state = _parse_live_state(payload)
        if state is not None:
            return state, _extract_room_id(payload) or room_id
    logger.warning(
        'TikTok @%s : détection REST indéterminée (live_status/room_id échoués). '
        'Sandbox Railway : lance le live manuellement + « Activer capture JP ».',
        normalized,
    )
    return None, room_id


def confirm_streamer_is_live(unique_id: str) -> bool | None:
    """Confirmation qu'un compte est EN live.

    - True  : live confirmé
    - False : offline confirmé
    - None  : indéterminé → ne rien faire
    """
    if not tiktool_configured() or _tiktool_is_rate_limited():
        return None
    normalized = normalize_tiktok_username(unique_id)
    if not _is_valid_unique_id(normalized):
        return None
    return _detect_live_status(normalized)[0]


def confirm_streamer_is_offline(unique_id: str) -> bool | None:
    """Confirmation fiable qu'un compte est HORS live.

    - True  : offline confirmé
    - False : encore en live
    - None  : indéterminé → ne pas clôturer
    """
    hint = confirm_streamer_is_live(unique_id)
    if hint is True:
        return False
    if hint is False:
        return True
    return None


def check_streamer_is_live(unique_id: str, *, deep: bool = False) -> bool | None:
    """Statut live TikTok (live_status prioritaire, room_id en secours)."""
    if not tiktool_configured() or _tiktool_is_rate_limited():
        return None
    normalized = normalize_tiktok_username(unique_id)
    if not _is_valid_unique_id(normalized):
        logger.warning(
            'TikTok unique_id invalide: %r (attendu ex: azplus.mg)',
            unique_id,
        )
        return None
    return _detect_live_status(normalized)[0]


def build_tiktok_diffusion(live: Live) -> dict[str, Any] | None:
    username = live.vendeur.tiktok_username
    if not username:
        return None

    unique_id = normalize_tiktok_username(username)
    # Pas d'appel TikTools ici : le démarrage live + scouts WS suffisent.
    return {
        'username': username,
        'unique_id': unique_id,
        'status': 'PENDING_MANUAL',
        'is_live_on_tiktok': None,
        'tiktool_listener': tiktool_configured(),
        'demo': False,
        'instructions': (
            'Lancez le live sur TikTok (app ou Live Center). '
            'Les commentaires JP seront capturés automatiquement via TikTools '
            'et une réponse avec le lien formulaire sera publiée dans le chat live.'
        ),
    }


def _upsert_tiktok_diffusion(
    live: Live,
    *,
    unique_id: str,
    username: str | None = None,
    status: str = 'LIVE',
    is_live: bool | None = True,
    listener: str | None = None,
) -> Live:
    diffusion = dict(live.diffusion_plateformes or {})
    current = dict(diffusion.get('tiktok') or {})
    merged = {
        **current,
        'status': status,
        'is_live_on_tiktok': is_live,
        'unique_id': unique_id,
        'username': username or current.get('username') or live.vendeur.tiktok_username,
        'demo': False,
        'updated_at': timezone.now().isoformat(),
    }
    if listener:
        merged['listener'] = listener
    diffusion['tiktok'] = merged
    live.diffusion_plateformes = diffusion
    live.save(update_fields=['diffusion_plateformes'])
    return live


def build_tiktok_confirmation_comment(live: Live) -> str:
    from .order_messaging import public_order_form_url

    return (
        "📦 Pour confirmer votre commande, cliquez ici :\n"
        f"{public_order_form_url(live.id)}"
    )


def _parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None


def ensure_tiktok_confirmation_comment(live: Live, *, force: bool = False) -> dict[str, Any]:
    """Génère le lien/commentaire de confirmation dès détection du live.

    Flux principal (recommandé) :
    - le backend génère et stocke le lien + texte à coller ;
    - le vendeur copie depuis l'UI AZLive, colle et épingle manuellement sur TikTok.

    Option secondaire (si TIKTOK_SESSION_COOKIES est configuré) :
    - tentative d'envoi auto via TikTools chat-send (non officiel, fragile).
    """
    if live.statut != Live.STATUT_EN_COURS:
        return {'sent': False, 'detail': 'Live non actif.'}

    from .order_messaging import public_order_form_url
    from .tiktok_live_chat import send_tiktok_live_chat_message, tiktok_chat_send_configured

    diffusion = dict(live.diffusion_plateformes or {})
    tiktok_state = dict(diffusion.get('tiktok') or {})
    content = build_tiktok_confirmation_comment(live)
    link = public_order_form_url(live.id)
    now = timezone.now()

    # Toujours générer le lien (indépendamment des cookies).
    tiktok_state.update(
        {
            'confirmation_link': link,
            'confirmation_comment': content,
            'pin_supported': False,
            'pin_mode': 'manual',
            'pin_note': (
                'Copiez le commentaire depuis AZLive, collez-le dans le chat TikTok '
                'puis épinglez-le manuellement. Aucune API officielle ne permet le pin auto.'
            ),
        }
    )

    delivery: dict[str, Any] = {
        'sent': False,
        'mode': 'manual_copy',
        'confirmation_link': link,
        'confirmation_comment': content,
        'detail': 'Lien prêt à copier/épingler manuellement.',
    }

    # Envoi auto facultatif uniquement si cookies session configurés.
    if live.vendeur.tiktok_username and tiktok_chat_send_configured():
        cooldown_minutes = int(getattr(settings, 'TIKTOK_CONFIRMATION_COMMENT_REFRESH_MINUTES', 10))
        last_sent_at = _parse_iso_dt(tiktok_state.get('confirmation_comment_sent_at'))
        cooldown_ok = (
            force
            or last_sent_at is None
            or (now - last_sent_at) >= timedelta(minutes=max(cooldown_minutes, 1))
        )
        if cooldown_ok:
            delivery = send_tiktok_live_chat_message(live.vendeur.tiktok_username, content)
            delivery['mode'] = 'auto_chat_send'
            delivery['confirmation_link'] = link
            delivery['confirmation_comment'] = content
            if delivery.get('sent'):
                tiktok_state['confirmation_comment_sent_at'] = now.isoformat()
        else:
            delivery = {
                'sent': False,
                'skipped': True,
                'mode': 'auto_chat_send',
                'confirmation_link': link,
                'confirmation_comment': content,
                'detail': f'Cooldown actif ({cooldown_minutes} min).',
            }

    tiktok_state['confirmation_comment_delivery'] = delivery
    tiktok_state['confirmation_link_generated_at'] = now.isoformat()
    diffusion['tiktok'] = tiktok_state
    live.diffusion_plateformes = diffusion
    live.save(update_fields=['diffusion_plateformes'])
    return delivery


def build_tiktok_live_title(unique_id: str, when=None) -> str:
    """Nom auto : Live - TikTok - {compte} - {YYYY-MM-DD HH:mm:ss} (heure Madagascar)."""
    from zoneinfo import ZoneInfo

    moment = when or timezone.now()
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment, timezone.utc)
    local = moment.astimezone(ZoneInfo('Indian/Antananarivo'))
    return f'Live - TikTok - {unique_id} - {local.strftime("%Y-%m-%d %H:%M:%S")}'


def ensure_tiktok_live_for_streamer(
    streamer_unique_id: str,
    *,
    already_verified: bool = False,
) -> Live | None:
    """Crée/active un Live AZLive quand TikTok est réellement en direct.

    - already_verified=False (roomInfo / sync faible) : exige POST room_id alive=True.
    - already_verified=True (chat / gift / streamStart) : confirme via room_id si
      possible ; si quota REST, on fait confiance au signal fort WS.
    """
    unique_id = normalize_tiktok_username(streamer_unique_id)
    vendeur = resolve_vendeur_from_tiktok_username(unique_id)
    if not vendeur:
        for candidate, uid in iter_connected_tiktok_vendeurs():
            if uid == unique_id:
                vendeur = candidate
                break
    if not vendeur:
        logger.warning(
            'Aucun vendeur AZLive pour @%s (tiktok_username ou compte connecté)',
            unique_id,
        )
        return None

    existing = (
        Live.objects.filter(vendeur=vendeur, statut=Live.STATUT_EN_COURS)
        .order_by('-date_live')
        .first()
    )
    if existing is not None:
        # Live déjà ouvert : pas de gate REST supplémentaire ici.
        pass
    elif already_verified:
        # Signal fort WS : refuser seulement si room_id dit offline clairement.
        if not _tiktool_is_rate_limited():
            verified = _check_live_via_room_id(unique_id)[0]
            if verified is False:
                logger.info(
                    'Pas de création Live pour @%s : signal WS fort mais room_id offline',
                    unique_id,
                )
                return None
    else:
        # Signal faible (roomInfo…) : confirmation room_id obligatoire.
        verified = confirm_streamer_is_live(unique_id)
        if verified is not True:
            logger.info(
                'Pas de création Live pour @%s : live TikTok non confirmé (%s)',
                unique_id,
                verified,
            )
            return None

    now = timezone.now()

    live = (
        Live.objects.filter(vendeur=vendeur, statut=Live.STATUT_EN_COURS)
        .order_by('-date_live')
        .first()
    )
    if live:
        live = _upsert_tiktok_diffusion(
            live,
            unique_id=unique_id,
            username=vendeur.tiktok_username,
            status='LIVE',
            is_live=True,
        )
        # Listener d'abord : la génération du lien ne doit pas bloquer la capture JP.
        ensure_tiktool_listener(live)
        try:
            ensure_tiktok_confirmation_comment(live)
        except Exception:
            logger.exception('Confirmation link non généré pour live #%s', live.pk)
        return live

    # Réutilise en priorité un live planifié récent du vendeur (dressing déjà préparé).
    window_start = now - timedelta(hours=24)
    live = (
        Live.objects.filter(
            vendeur=vendeur,
            statut=Live.STATUT_PLANIFIE,
            date_live__gte=window_start,
        )
        .order_by('date_live')
        .first()
    )
    auto_title = build_tiktok_live_title(unique_id, now)
    if live is None:
        live = Live.objects.create(
            titre=auto_title,
            vendeur=vendeur,
            statut=Live.STATUT_EN_COURS,
            date_live=now,
            date_debut=now,
        )
    else:
        live.titre = auto_title
        live.statut = Live.STATUT_EN_COURS
        live.date_debut = live.date_debut or now
        live.date_live = now
        live.date_fin = None
        live.save(update_fields=['titre', 'statut', 'date_debut', 'date_live', 'date_fin'])

    live = _upsert_tiktok_diffusion(
        live,
        unique_id=unique_id,
        username=vendeur.tiktok_username,
        status='LIVE',
        is_live=True,
    )
    ensure_tiktool_listener(live)
    try:
        ensure_tiktok_confirmation_comment(live, force=True)
    except Exception:
        logger.exception('Confirmation link non généré pour live #%s', live.pk)
    return live


def process_tiktool_chat_event(streamer_unique_id: str, event_data: dict[str, Any]) -> dict[str, Any]:
    user = event_data.get('user') or {}
    sender_id = str(user.get('uniqueId') or user.get('userId') or user.get('id') or '')
    sender_name = user.get('nickname') or user.get('uniqueId') or 'Client TikTok'
    comment_text = event_data.get('comment') or event_data.get('text') or ''

    vendeur = resolve_vendeur_from_tiktok_username(streamer_unique_id)
    # Un commentaire chat n'arrive que si le room est actif → preuve suffisante.
    live = (
        ensure_tiktok_live_for_streamer(streamer_unique_id, already_verified=True)
        if vendeur
        else None
    )
    if live is None and vendeur:
        live = resolve_active_live(vendeur)

    result = process_social_comment(
        sender_id=sender_id,
        sender_name=sender_name,
        comment_text=comment_text,
        channel='TikTok',
        vendeur=vendeur,
        live=live,
        id_field='tiktok_id',
    )
    if live is not None and 'live_id' not in result:
        result = {**result, 'live_id': live.id}
    return result


def _build_ws_url(unique_id: str) -> str:
    params = urllib.parse.urlencode(
        {
            'uniqueId': normalize_tiktok_username(unique_id),
            'apiKey': settings.TIKTOOL_API_KEY,
        }
    )
    return f'{TIKTOOL_WS_BASE}?{params}'


class _TikToolLiveListener(threading.Thread):
    """Scout WS : détection début + fin de live TikTok en parallèle.

    Début : chat / gift / streamStart (fort) → create ; roomInfo (faible) → REST room_id
    Fin   : streamEnd / control(action=3) / close 4005|… confirmés via room_id offline
            + après reconnect : pas d'activité forte + room_id offline (pas live_status seul)
    """

    daemon = True

    def __init__(
        self,
        live_id: int | None,
        unique_id: str,
        stop_event: threading.Event,
        *,
        scout: bool = False,
    ):
        super().__init__(name=f'tiktool-{"scout" if scout else "live"}-{unique_id}')
        self.live_id = live_id
        self.unique_id = normalize_tiktok_username(unique_id)
        self.stop_event = stop_event
        self.scout = scout
        # Scout : départ plus lent pour économiser les sessions WS Sandbox.
        self._reconnect_delay = 120.0 if scout else 20.0
        self._last_close_code: int | None = None
        self._session_saw_live = False
        self._stream_end_event_seen = False
        # True = on vient de reconnecter avec un Live AZLive encore en_cours :
        # on attend chat/gift/viewers (pas roomInfo) avant de confirmer qu'il tourne.
        self._verify_still_live = False
        self._got_activity_proof = False
        self._proof_timer: threading.Timer | None = None

    def run(self):
        try:
            import websocket
        except ImportError:
            logger.error('websocket-client non installé: pip install websocket-client')
            return

        while not self.stop_event.is_set():
            if _tiktool_ws_is_rate_limited():
                wait = max(tiktool_ws_rate_limit_remaining_seconds(), 30.0)
                logger.info(
                    'Scout @%s en pause WS (quota horaire), prochain essai dans %.0fs',
                    self.unique_id,
                    wait,
                )
                if self.stop_event.wait(min(wait, 600.0)):
                    break
                continue

            # Mode économie : hors live AZLive, ne pas ouvrir de WS (REST détecte).
            if _ws_on_demand():
                try:
                    close_old_connections()
                    active = _find_active_tiktok_live_for_streamer(self.unique_id)
                except Exception:
                    active = None
                if active is None:
                    logger.info(
                        'TikTools on_demand : pas de live actif @%s — arrêt WS (0 session)',
                        self.unique_id,
                    )
                    break

            ws_app = websocket.WebSocketApp(
                _build_ws_url(self.unique_id),
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
                on_open=self._on_open,
            )
            ws_app.run_forever(ping_interval=30, ping_timeout=10)

            # Après fermeture : en on_demand, stop si plus de live (évite reconnexions).
            if _ws_on_demand():
                try:
                    close_old_connections()
                    still_active = _find_active_tiktok_live_for_streamer(self.unique_id)
                except Exception:
                    still_active = None
                if still_active is None or self._last_close_code in _WS_STREAM_END_CODES or self._stream_end_event_seen:
                    logger.info(
                        'TikTools on_demand : fin WS @%s (live terminé / absent) — pas de reconnexion',
                        self.unique_id,
                    )
                    break

            if self._last_close_code in _WS_STREAM_END_CODES or self._stream_end_event_seen:
                # Fin explicite : retenter bientôt seulement hors on_demand.
                delay = 45.0 if self.scout else 8.0
                self._reconnect_delay = delay
            elif self._last_close_code == 4429 or _tiktool_ws_is_rate_limited():
                delay = max(
                    self._reconnect_delay,
                    tiktool_ws_rate_limit_remaining_seconds(),
                    _ws_cooldown_seconds(),
                )
            else:
                # Coupure réseau (1006…) : backoff très agressif (1 session = 1 quota).
                delay = self._reconnect_delay
                self._reconnect_delay = min(
                    self._reconnect_delay * 2.0,
                    600.0 if self.scout else 180.0,
                )
                if self.scout:
                    delay = max(delay, 120.0)
                else:
                    try:
                        close_old_connections()
                        if _find_active_tiktok_live_for_streamer(self.unique_id):
                            delay = min(delay, 20.0)
                    except Exception:
                        pass

            logger.info(
                'TikTools WS reconnexion @%s dans %.0fs (dernier code=%s)',
                self.unique_id,
                delay,
                self._last_close_code,
            )
            if self.stop_event.wait(delay):
                break

        # Thread terminé : libérer le slot pool + promouvoir la file.
        self._release_ws_pool_slot()

    def _release_ws_pool_slot(self) -> None:
        """Retire ce listener des registres et ouvre le prochain live en file."""
        with _listeners_lock:
            if _scouts.get(self.unique_id) is self:
                _scouts.pop(self.unique_id, None)
            if self.live_id and _listeners.get(self.live_id) is self:
                _listeners.pop(self.live_id, None)
            promoted = _ws_promote_pending_locked()
            if promoted:
                logger.info(
                    'Pool WS : @%s libéré → %s live(s) promu(s) (actifs=%s/%s)',
                    self.unique_id,
                    promoted,
                    _ws_active_count_locked(),
                    _ws_max_connections(),
                )

    def _cancel_proof_timer(self) -> None:
        timer = self._proof_timer
        self._proof_timer = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass

    def _note_activity(self) -> None:
        """Activité réelle reçue → le live TikTok tourne encore."""
        self._got_activity_proof = True
        self._verify_still_live = False
        self._session_saw_live = True
        self._stream_end_event_seen = False
        self._cancel_proof_timer()

    def _schedule_verify_or_end(self, seconds: float = 45.0) -> None:
        """Après reconnect avec Live AZLive encore ouvert : activité ou clôture.

        roomInfo / roomUserSeq n'annulent PAS ce timer — seuls chat/gift/streamStart.
        Clôture uniquement si POST room_id dit offline (jamais live_status seul).
        """
        self._cancel_proof_timer()
        self._got_activity_proof = False
        self._verify_still_live = True

        def _check() -> None:
            try:
                close_old_connections()
                if self.stop_event.is_set() or self._got_activity_proof:
                    return
                active = _find_active_tiktok_live_for_streamer(self.unique_id)
                if active is None:
                    self._verify_still_live = False
                    return

                # Confirmation fiable uniquement via room_id (pas live_status cache).
                offline = confirm_streamer_is_offline(self.unique_id)
                if offline is False:
                    logger.info(
                        'Reconnect @%s : room_id encore alive → on garde AZLive #%s',
                        self.unique_id,
                        active.pk,
                    )
                    self._note_activity()
                    self.live_id = active.pk
                    return
                if offline is True:
                    logger.warning(
                        'Reconnect @%s : pas d\'activité WS + room_id offline '
                        '→ clôture AZLive #%s',
                        self.unique_id,
                        active.pk,
                    )
                    self.live_id = active.pk
                    self._end_live_from_ws_signal('reconnect_no_activity')
                    return

                logger.info(
                    'Reconnect @%s : aucune activité mais statut indéterminé '
                    '— AZLive #%s laissé en_cours',
                    self.unique_id,
                    active.pk,
                )
                self._verify_still_live = False
            except Exception:
                logger.exception('verify-timeout @%s', self.unique_id)

        self._proof_timer = threading.Timer(seconds, _check)
        self._proof_timer.daemon = True
        self._proof_timer.start()

    def _on_open(self, _ws):
        # Ne pas reset à 15s : une session réussie ne doit pas relancer un spam
        # de reconnexions après le prochain drop réseau.
        self._reconnect_delay = 120.0 if self.scout else 20.0
        self._last_close_code = None
        self._stream_end_event_seen = False
        logger.info('TikTools WS connecté (@%s mode=%s)', self.unique_id, _ws_mode())

        try:
            close_old_connections()
            active = _find_active_tiktok_live_for_streamer(self.unique_id)
            if active:
                # Cas fin / drop WS : on vérifie que TikTok tourne vraiment encore.
                self.live_id = active.pk
                self._session_saw_live = True
                logger.info(
                    'Scout @%s : live AZLive #%s encore en_cours — '
                    'attente activité forte (chat/gift) 45s',
                    self.unique_id,
                    active.pk,
                )
                self._schedule_verify_or_end(45.0)
            else:
                # Cas début : roomInfo / chat créera le live dès qu'il démarre.
                self.live_id = None
                self._session_saw_live = False
                self._got_activity_proof = False
                self._verify_still_live = False
                self._cancel_proof_timer()
        except Exception:
            logger.exception('Réattachment live après WS open (@%s)', self.unique_id)
            self._session_saw_live = False
            self._verify_still_live = False

    def _ensure_live_from_ws_signal(self, reason: str, *, strong: bool = True) -> None:
        """Début (ou confirmation) de live.

        strong=True  : chat/gift/streamStart — preuve WS + room_id si dispo
        strong=False : roomInfo/roomUserSeq — exige confirmation REST room_id
        """
        if strong:
            self._note_activity()
        try:
            live = ensure_tiktok_live_for_streamer(
                self.unique_id,
                already_verified=strong,
            )
            if live:
                if not strong:
                    self._note_activity()
                self.live_id = live.pk
                logger.info(
                    'Live AZLive #%s créé/activé via WS %s (@%s strong=%s)',
                    live.pk,
                    reason,
                    self.unique_id,
                    strong,
                )
            elif not strong:
                logger.info(
                    'WS %s @%s ignoré : room_id non confirmé (pas de création)',
                    reason,
                    self.unique_id,
                )
        except Exception:
            logger.exception('ensure live via WS %s (@%s)', reason, self.unique_id)

    def _confirm_end_from_ws_signal(self, reason: str) -> None:
        """Fin WS signalée → clôturer seulement si room_id confirme offline."""
        try:
            close_old_connections()
            offline = confirm_streamer_is_offline(self.unique_id)
            if offline is False:
                logger.warning(
                    'Fin WS %s ignorée @%s : room_id encore alive',
                    reason,
                    self.unique_id,
                )
                self._stream_end_event_seen = False
                self._note_activity()
                return
            if offline is None:
                logger.info(
                    'Fin WS %s @%s indéterminée → verify 45s avant clôture',
                    reason,
                    self.unique_id,
                )
                active = _find_active_tiktok_live_for_streamer(self.unique_id)
                if active:
                    self.live_id = active.pk
                    self._schedule_verify_or_end(45.0)
                return
            self._end_live_from_ws_signal(reason)
        except Exception:
            logger.exception('confirm end via WS %s (@%s)', reason, self.unique_id)

    def _end_live_from_ws_signal(self, reason: str) -> None:
        """Fin TikTok confirmée → passer le live AZLive en terminé (archives)."""
        self._cancel_proof_timer()
        self._verify_still_live = False
        try:
            close_old_connections()
            closed = cloturer_tiktok_lives_for_streamer(self.unique_id, reason=reason)
            if self.live_id:
                live = (
                    Live.objects.filter(pk=self.live_id, statut=Live.STATUT_EN_COURS)
                    .select_related('vendeur')
                    .first()
                )
                if live and cloturer_tiktok_live(live, reason=reason):
                    closed += 1
            self.live_id = None
            self._session_saw_live = False
            self._stream_end_event_seen = False
            self._got_activity_proof = False
            logger.info(
                'Fin live TikTok via WS %s (@%s) → %s session(s) clôturée(s)/archivée(s)',
                reason,
                self.unique_id,
                closed,
            )
        except Exception:
            logger.exception('clôture live via WS %s (@%s)', reason, self.unique_id)

    def _on_message(self, _ws, message: str):
        close_old_connections()
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            return

        if not isinstance(payload, dict):
            return

        event = str(payload.get('event') or '').strip()
        if not event and isinstance(payload.get('data'), dict):
            event = str(payload['data'].get('type') or '').strip()
        data = payload.get('data') if isinstance(payload.get('data'), dict) else {}

        # ——— FIN ———
        if event in {'streamEnd', 'stream_end', 'liveEnd', 'live_end'}:
            logger.info('TikTools streamEnd (@%s) data=%s', self.unique_id, data or payload)
            self._stream_end_event_seen = True
            self._confirm_end_from_ws_signal(event)
            return

        if event == 'control':
            try:
                action = int(data.get('action') if data else payload.get('action') or -1)
            except (TypeError, ValueError):
                action = -1
            if action == 3:
                logger.info('TikTools control action=3 stream end (@%s)', self.unique_id)
                self._stream_end_event_seen = True
                self._confirm_end_from_ws_signal('control_stream_end')
            return

        room_id = (
            payload.get('roomId')
            or payload.get('room_id')
            or data.get('roomId')
            or data.get('room_id')
        )

        # ——— DÉBUT via roomInfo (faible) ———
        # TikTools envoie souvent un roomInfo+roomId hors live → REST obligatoire.
        # En verify_still_live : n'annule jamais le timer de fin.
        if event in {'roomInfo', 'room_info'}:
            if self._verify_still_live:
                logger.debug(
                    'roomInfo ignoré pendant verify_still_live (@%s roomId=%s)',
                    self.unique_id,
                    room_id,
                )
                return
            if not self.live_id and room_id:
                logger.info(
                    'TikTools roomInfo → tentative début (@%s roomId=%s, confirm REST)',
                    self.unique_id,
                    room_id,
                )
                self._ensure_live_from_ws_signal('roomInfo', strong=False)
            elif self.live_id and room_id:
                self._session_saw_live = True
            return

        # ——— Signaux faibles (viewers seq) : jamais preuve « encore live » ———
        if event in _WEAK_LIVE_EVENTS:
            if not self.live_id and not self._verify_still_live:
                logger.info(
                    'TikTools signal faible (@%s event=%s) → confirm REST',
                    self.unique_id,
                    event,
                )
                self._ensure_live_from_ws_signal(event, strong=False)
            return

        # ——— Signaux forts = début OU confirmation « encore live » ———
        if event in _STRONG_LIVE_EVENTS and event != 'chat':
            if not self.live_id or self._verify_still_live:
                logger.info(
                    'TikTools signal fort (@%s event=%s roomId=%s)',
                    self.unique_id,
                    event,
                    room_id,
                )
                self._ensure_live_from_ws_signal(event, strong=True)
            else:
                self._note_activity()
            return

        if event != 'chat':
            return

        if not self.live_id or self._verify_still_live:
            self._ensure_live_from_ws_signal('chat', strong=True)
        else:
            self._note_activity()

        event_data = data or {}
        try:
            result = process_tiktool_chat_event(self.unique_id, event_data)
            if result.get('live_id'):
                self.live_id = result.get('live_id')
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
        err_txt = str(error or '')
        if '4429' in err_txt or 'rate limit' in err_txt.lower():
            _mark_ws_rate_limited()
            self._last_close_code = 4429
        logger.warning('TikTools WebSocket error (@%s / live #%s): %s', self.unique_id, self.live_id, error)

    def _on_close(self, _ws, close_status_code, close_msg):
        self._last_close_code = close_status_code
        reason = str(close_msg or '')
        if close_status_code == 4429 or 'rate limit' in reason.lower():
            _mark_ws_rate_limited()

        # Uniquement les codes fin explicites TikTools — PAS 1006 (coupure réseau).
        # Même pour ces codes : confirmation room_id (évite fausse clôture).
        should_end = (
            close_status_code in _WS_STREAM_END_CODES
            or self._stream_end_event_seen
        )

        if should_end:
            close_old_connections()
            self._confirm_end_from_ws_signal(f'ws_close_{close_status_code}')
        else:
            self._cancel_proof_timer()
            # Sur drop réseau avec live encore ouvert : au prochain open,
            # verify_still_live + activité forte décidera de clôturer ou non.

        logger.info(
            'TikTools WebSocket fermé (@%s / live #%s): %s %s',
            self.unique_id,
            self.live_id,
            close_status_code,
            close_msg,
        )


def _start_listener_locked(unique_id: str, live_id: int | None = None, *, scout: bool = False) -> '_TikToolLiveListener':
    stop_event = threading.Event()
    listener = _TikToolLiveListener(live_id, unique_id, stop_event, scout=scout)
    if scout:
        old = _scouts.get(unique_id)
        if old and old is not listener:
            old.stop_event.set()
        _scouts[unique_id] = listener
    if live_id:
        old_live = _listeners.get(live_id)
        if old_live and old_live is not listener and not old_live.scout:
            old_live.stop_event.set()
        _listeners[live_id] = listener
        _ws_remove_pending_locked(live_id)
    listener.start()
    return listener


def start_tiktool_listener(live: Live) -> bool:
    """Demande un slot WS pour un live.

    - Slot libre → démarre immédiatement
    - Pool plein → mise en file FIFO (démarrera quand un live libère un slot)
    - Réutilise un scout déjà ouvert pour le même @ (0 nouvelle session)
    """
    if not tiktool_configured() or live.vendeur.is_demo_mode:
        return False

    username = live.vendeur.tiktok_username
    if not username:
        return False

    if _tiktool_ws_is_rate_limited():
        logger.warning(
            'Pool WS : live #%s en file d\'attente (quota TikTools 4429, reste ~%.0fs)',
            live.pk,
            tiktool_ws_rate_limit_remaining_seconds(),
        )
        with _listeners_lock:
            pos = _ws_enqueue_pending_locked(
                live.pk,
                normalize_tiktok_username(username),
            )
        logger.info('Pool WS : live #%s position file=%s (attente reset quota)', live.pk, pos)
        return False

    unique_id = normalize_tiktok_username(username)
    with _listeners_lock:
        # Réutilise le scout déjà connecté pour cet unique_id (évite 2 WS).
        scout = _scouts.get(unique_id)
        if scout and scout.is_alive() and not scout.stop_event.is_set():
            scout.live_id = live.pk
            scout.scout = True
            _listeners[live.pk] = scout
            _ws_remove_pending_locked(live.pk)
            logger.info('TikTools scout réutilisé pour live #%s (@%s)', live.pk, unique_id)
            return True

        # Déjà un WS actif pour un autre compte et pool saturé → file d'attente.
        already_counted = unique_id in _ws_active_unique_ids_locked()
        if not already_counted and not _ws_pool_has_slot_locked():
            pos = _ws_enqueue_pending_locked(live.pk, unique_id)
            logger.warning(
                'Pool WS plein (%s/%s) : live #%s (@%s) en file (position %s)',
                _ws_active_count_locked(),
                _ws_max_connections(),
                live.pk,
                unique_id,
                pos,
            )
            print(
                f'\n[TIKTOOL] Pool WS plein ({_ws_active_count_locked()}/{_ws_max_connections()}). '
                f'Live #{live.pk} @{unique_id} en attente (#{pos}).\n',
                flush=True,
            )
            return False

        stop_tiktool_listener(live, lock_held=True, promote=False)
        if unique_id not in _ws_active_unique_ids_locked() and not _ws_pool_has_slot_locked():
            pos = _ws_enqueue_pending_locked(live.pk, unique_id)
            logger.warning(
                'Pool WS plein après arrêt local (%s/%s) : live #%s en file (#%s)',
                _ws_active_count_locked(),
                _ws_max_connections(),
                live.pk,
                pos,
            )
            return False
        _start_listener_locked(unique_id, live.pk, scout=True)

    logger.info(
        'TikTools listener démarré pour live #%s (@%s) — pool %s/%s',
        live.pk,
        unique_id,
        tiktool_ws_pool_status()['active'],
        _ws_max_connections(),
    )
    return True


def stop_tiktool_listener(
    live: Live,
    lock_held: bool = False,
    *,
    promote: bool = True,
) -> bool:
    live_id = live.pk

    def _stop():
        _ws_remove_pending_locked(live_id)
        listener = _listeners.pop(live_id, None)
        if not listener:
            # Peut être seulement en file d'attente.
            return False
        # Scout du compte :
        # - always     → détacher le live, garder le WS pour le prochain direct
        # - on_demand  → fermer le WS (économie quota ; REST redétectera)
        freed = False
        if _scouts.get(listener.unique_id) is listener:
            if _ws_on_demand():
                listener.stop_event.set()
                _scouts.pop(listener.unique_id, None)
                freed = True
            else:
                listener.live_id = None
            if promote and freed:
                _ws_promote_pending_locked()
            return True
        listener.stop_event.set()
        if promote:
            _ws_promote_pending_locked()
        return True

    if lock_held:
        return _stop()

    with _listeners_lock:
        return _stop()


def ensure_tiktok_scouts(*, vendeur_id: int | None = None) -> int:
    """Maintient un WebSocket TikTools par compte TikTok **connecté**.

    Mode `on_demand` (défaut) : n'ouvre un WS que s'il existe déjà un live EN_COURS
    pour ce vendeur (la détection hors-live passe par REST → 0 session WS).

    Mode `always` : scout permanent (réactif, mais brûle le quota Sandbox).
    """
    if not tiktool_configured():
        msg = (
            'TikTools: TIKTOOL_API_KEY manquant — détection live désactivée '
            '(connexion OAuth seule ne suffit pas).'
        )
        logger.warning(msg)
        print(f'\n[TIKTOOL] {msg}\n', flush=True)
        return 0
    if _tiktool_ws_is_rate_limited():
        remaining = tiktool_ws_rate_limit_remaining_seconds()
        logger.warning(
            'ensure_tiktok_scouts ignoré : quota WS (reste ~%.0fs) — '
            'le watchdog utilisera le REST pour détecter les lives',
            remaining,
        )
        print(
            f'\n[TIKTOOL] Scout WS en pause (reste ~{remaining:.0f}s). '
            f'Détection via REST active.\n',
            flush=True,
        )
        return 0
    started = 0
    connected = list(iter_connected_tiktok_vendeurs(vendeur_id=vendeur_id))
    if not connected:
        # Explique pourquoi la détection « ne marche pas » alors que OAuth OK.
        qs = Vendeur.objects.exclude(tiktok_open_id__isnull=True).exclude(tiktok_open_id='')
        if vendeur_id is not None:
            qs = qs.filter(pk=vendeur_id)
        details = []
        for v in qs[:10]:
            raw = v.tiktok_username or ''
            norm = normalize_tiktok_username(raw)
            if not raw:
                reason = 'tiktok_username vide'
            elif not _is_valid_unique_id(norm):
                reason = f'handle invalide {raw!r} (attendu ex. azplus.mg)'
            else:
                reason = 'ok mais non résolu'
            details.append(f'#{v.pk}:{reason}')
        msg = (
            'TikTools: 0 scout — aucun compte OAuth avec @ valide. '
            + ('; '.join(details) if details else 'Aucun vendeur avec tiktok_open_id.')
        )
        logger.warning(msg)
        print(f'\n[TIKTOOL] {msg}\n', flush=True)
        return 0

    on_demand = _ws_on_demand()
    if on_demand and _ws_manual_capture():
        logger.info(
            'TikTools WS manual_capture : ensure_tiktok_scouts ne démarre rien '
            '(attendre le bouton « Activer capture JP »)'
        )
        return 0

    if on_demand:
        logger.info(
            'TikTools WS mode=on_demand : scouts seulement pour lives EN_COURS '
            '(détection hors-live via REST)'
        )

    for vendeur, unique_id in connected:
        active_live = (
            Live.objects.filter(vendeur=vendeur, statut=Live.STATUT_EN_COURS)
            .order_by('-date_live')
            .first()
        )
        if on_demand and active_live is None:
            # Ferme un éventuel scout orphelin hors live.
            with _listeners_lock:
                orphan = _scouts.get(unique_id)
                if orphan and orphan.is_alive() and orphan.live_id is None:
                    orphan.stop_event.set()
                    _scouts.pop(unique_id, None)
            continue

        with _listeners_lock:
            existing = _scouts.get(unique_id)
            if existing and existing.is_alive() and not existing.stop_event.is_set():
                continue

            # Respecter le plafond pool (même en mode always).
            if unique_id not in _ws_active_unique_ids_locked() and not _ws_pool_has_slot_locked():
                if on_demand and active_live is not None:
                    pos = _ws_enqueue_pending_locked(active_live.pk, unique_id)
                    logger.warning(
                        'Pool WS plein : @%s (live #%s) en file position %s',
                        unique_id,
                        active_live.pk,
                        pos,
                    )
                else:
                    logger.info(
                        'Pool WS plein (%s/%s) : scout @%s non démarré',
                        _ws_active_count_locked(),
                        _ws_max_connections(),
                        unique_id,
                    )
                continue

            _start_listener_locked(
                unique_id,
                live_id=active_live.pk if active_live else None,
                scout=True,
            )
            started += 1
            logger.info(
                'TikTools scout démarré pour @%s (vendeur #%s, mode=%s, pool=%s/%s)',
                unique_id,
                vendeur.pk,
                _ws_mode(),
                _ws_active_count_locked(),
                _ws_max_connections(),
            )
            print(
                f'\n[TIKTOOL] Scout démarré pour @{unique_id} '
                f'(vendeur #{vendeur.pk}, mode={_ws_mode()}, '
                f'pool={_ws_active_count_locked()}/{_ws_max_connections()})\n',
                flush=True,
            )
    return started


def listener_status(live_id: int) -> dict[str, Any]:
    with _listeners_lock:
        listener = _listeners.get(live_id)
        if not listener:
            return {'running': False}
        return {
            'running': listener.is_alive(),
            'unique_id': listener.unique_id,
            'thread': listener.name,
            'scout': listener.scout,
        }


def ensure_tiktool_listener(live: Live, *, force: bool = False) -> bool:
    """Démarre/re-démarre le listener TikTok pour un live en cours.

    - force=False (chemins auto sync/recover) : ignoré si TIKTOOL_WS_MANUAL_CAPTURE
    - force=True  : bouton vendeur « Activer capture JP »
    """
    if live.statut != Live.STATUT_EN_COURS or live.vendeur.is_demo_mode:
        return False
    if _ws_manual_capture() and not force:
        logger.debug(
            'ensure_tiktool_listener live #%s ignoré (manual capture — attendre le bouton vendeur)',
            live.pk,
        )
        return False
    status = listener_status(live.pk)
    if status.get('running'):
        return True
    started = start_tiktool_listener(live)
    if started and live.vendeur.tiktok_username:
        _upsert_tiktok_diffusion(
            live,
            unique_id=normalize_tiktok_username(live.vendeur.tiktok_username),
            username=live.vendeur.tiktok_username,
            status='LIVE',
            is_live=True,
            listener='running',
        )
        try:
            ensure_tiktok_confirmation_comment(live)
        except Exception:
            logger.exception(
                'Confirmation link non généré après démarrage listener live #%s',
                live.pk,
            )
    return started


def capture_jp_status_for_live(live: Live) -> dict[str, Any]:
    """État capture JP (WS) pour un live — UI vendeur."""
    pool = tiktool_ws_pool_status()
    ls = listener_status(live.pk)
    pending_pos = None
    with _listeners_lock:
        for idx, (lid, _) in enumerate(_ws_pending):
            if lid == live.pk:
                pending_pos = idx + 1
                break
    return {
        'live_id': live.pk,
        'capture_active': bool(ls.get('running')),
        'unique_id': ls.get('unique_id') or (
            normalize_tiktok_username(live.vendeur.tiktok_username or '')
            if live.vendeur_id
            else ''
        ),
        'manual_capture': _ws_manual_capture(),
        'ws_mode': _ws_mode(),
        'queued': pending_pos is not None,
        'queue_position': pending_pos,
        'pool': {
            'active': pool['active'],
            'max': pool['max_connections'],
            'pending': pool['pending'],
        },
        'ws_rate_limited': pool['ws_rate_limited'],
        'ws_rate_limit_remaining_seconds': pool['ws_rate_limit_remaining_seconds'],
        'tiktool_configured': tiktool_configured(),
    }


def start_capture_jp_for_live(live: Live) -> dict[str, Any]:
    """Action bouton vendeur : active la capture JP (ouvre un slot WS)."""
    if live.statut != Live.STATUT_EN_COURS:
        return {
            'ok': False,
            'detail': 'Le live doit être en cours pour activer la capture JP.',
            'status': capture_jp_status_for_live(live),
        }
    if not live.vendeur_id or not live.vendeur.tiktok_username:
        return {
            'ok': False,
            'detail': 'Aucun compte TikTok lié à ce vendeur.',
            'status': capture_jp_status_for_live(live),
        }
    if not tiktool_configured():
        return {
            'ok': False,
            'detail': 'TIKTOOL_API_KEY manquant sur le serveur.',
            'status': capture_jp_status_for_live(live),
        }

    started = ensure_tiktool_listener(live, force=True)
    status = capture_jp_status_for_live(live)
    if started:
        return {
            'ok': True,
            'detail': 'Capture JP activée — les commentaires TikTok sont écoutés.',
            'status': status,
        }
    if status.get('queued'):
        return {
            'ok': True,
            'detail': (
                f"Pool WS plein ({status['pool']['active']}/{status['pool']['max']}). "
                f"Live en file d'attente (position {status['queue_position']})."
            ),
            'status': status,
        }
    if status.get('ws_rate_limited'):
        return {
            'ok': False,
            'detail': (
                f"Quota WebSocket TikTools atteint "
                f"(reste ~{int(status['ws_rate_limit_remaining_seconds'])}s). "
                f"Réessaie après le reset."
            ),
            'status': status,
        }
    return {
        'ok': False,
        'detail': 'Impossible d\'activer la capture JP pour le moment.',
        'status': status,
    }


def stop_capture_jp_for_live(live: Live) -> dict[str, Any]:
    """Action bouton vendeur : coupe la capture JP et libère le slot pool."""
    stopped = stop_tiktool_listener(live)
    with _listeners_lock:
        _ws_remove_pending_locked(live.pk)
        _ws_promote_pending_locked()
    if live.vendeur_id and live.vendeur.tiktok_username:
        try:
            _upsert_tiktok_diffusion(
                live,
                unique_id=normalize_tiktok_username(live.vendeur.tiktok_username),
                username=live.vendeur.tiktok_username,
                status='LIVE',
                is_live=True,
                listener='stopped',
            )
        except Exception:
            logger.exception('Maj diffusion après stop capture JP live #%s', live.pk)
    status = capture_jp_status_for_live(live)
    return {
        'ok': True,
        'detail': 'Capture JP désactivée.' if stopped else 'Capture déjà inactive.',
        'stopped': bool(stopped),
        'status': status,
    }


def _facebook_still_live(live: Live) -> bool:
    broadcasts = list((live.diffusion_plateformes or {}).get('facebook') or [])
    for item in broadcasts:
        if str(item.get('status') or '').upper() in {'LIVE', 'LIVE_NOW'}:
            return True
    return False


def _find_active_tiktok_live_for_streamer(unique_id: str) -> Live | None:
    """Retrouve un live AZLive encore en_cours pour ce @TikTok."""
    normalized = normalize_tiktok_username(unique_id)
    vendeur = resolve_vendeur_from_tiktok_username(normalized)
    if not vendeur:
        for candidate, uid in iter_connected_tiktok_vendeurs():
            if uid == normalized:
                vendeur = candidate
                break
    if not vendeur:
        return None

    for live in (
        Live.objects.filter(vendeur=vendeur, statut=Live.STATUT_EN_COURS)
        .select_related('vendeur')
        .order_by('-date_live')
    ):
        if _live_is_tiktok_tracked(live):
            return live
        titre = (live.titre or '').lower()
        if normalized in titre or 'tiktok' in titre:
            return live
    return None


def _live_is_tiktok_tracked(live: Live) -> bool:
    tiktok_state = dict((live.diffusion_plateformes or {}).get('tiktok') or {})
    if not tiktok_state:
        return False
    # Toute diffusion TikTok non vide = live suivi (même si is_live_on_tiktok=False).
    return True


def cloturer_tiktok_live(live: Live, *, reason: str = 'tiktok_stream_end') -> bool:
    """Passe un live AZLive en terminé/archivé quand le direct TikTok s'arrête.

    Si Facebook est encore en live sur la même session, on ne clôture que la
    partie TikTok (statut AZLive reste en_cours).
    """
    if live.statut != Live.STATUT_EN_COURS:
        return False

    stop_tiktool_listener(live)

    diffusion = dict(live.diffusion_plateformes or {})
    tiktok_state = dict(diffusion.get('tiktok') or {})
    tiktok_state.update(
        {
            'status': 'ENDED',
            'is_live_on_tiktok': False,
            'listener': 'stopped',
            'ended_reason': reason,
            'updated_at': timezone.now().isoformat(),
        }
    )
    diffusion['tiktok'] = tiktok_state
    diffusion['stopped_at'] = timezone.now().isoformat()
    diffusion['stopped_reason'] = reason

    if _facebook_still_live(live):
        live.diffusion_plateformes = diffusion
        live.save(update_fields=['diffusion_plateformes'])
        logger.info(
            'TikTok terminé sur live #%s (%s) — Facebook encore live, statut AZLive inchangé',
            live.pk,
            reason,
        )
        return False

    # Clôture complète : terminé = archivé côté hub (Archives Terminées).
    try:
        from .facebook_live_comments import stop_facebook_comment_listener

        stop_facebook_comment_listener(live)
    except Exception:
        logger.exception('stop_facebook_comment_listener live #%s', live.pk)

    live.statut = Live.STATUT_TERMINE
    live.date_fin = timezone.now()
    live.diffusion_plateformes = diffusion
    live.save(update_fields=['statut', 'date_fin', 'diffusion_plateformes'])
    logger.info('Live #%s passé en terminé/archivé (%s)', live.pk, reason)
    return True


def cloturer_tiktok_lives_for_streamer(unique_id: str, *, reason: str = 'tiktok_stream_end') -> int:
    """Clôture tous les lives en_cours liés à ce @TikTok."""
    normalized = normalize_tiktok_username(unique_id)
    vendeur = resolve_vendeur_from_tiktok_username(normalized)
    if not vendeur:
        for candidate, uid in iter_connected_tiktok_vendeurs():
            if uid == normalized:
                vendeur = candidate
                break
    if not vendeur:
        return 0

    closed = 0
    active = list(
        Live.objects.filter(vendeur=vendeur, statut=Live.STATUT_EN_COURS)
        .select_related('vendeur')
        .order_by('-date_live')
    )
    for live in active:
        if not _live_is_tiktok_tracked(live):
            titre = (live.titre or '').lower()
            if normalized not in titre and 'tiktok' not in titre:
                continue
        before = live.statut
        cloturer_tiktok_live(live, reason=reason)
        live.refresh_from_db(fields=['statut'])
        if live.statut == Live.STATUT_TERMINE and before == Live.STATUT_EN_COURS:
            closed += 1
        elif before == Live.STATUT_EN_COURS and live.statut == Live.STATUT_EN_COURS:
            # TikTok marqué ENDED mais FB encore live → compte comme traité côté TikTok
            tiktok = dict((live.diffusion_plateformes or {}).get('tiktok') or {})
            if str(tiktok.get('status') or '').upper() == 'ENDED':
                closed += 1
    return closed


_last_end_reconcile_at: datetime | None = None


def reconcile_ended_tiktok_lives(*, min_interval_seconds: float = 60.0) -> int:
    """Filet : clôture un live AZLive encore en_cours si TikTok est offline.

    Utilisé quand `streamEnd` n'arrive pas.
    Ne se fie JAMAIS à `live_status=False` seul (cache ~90s → fausses clôtures).
    Clôture uniquement si POST room_id renvoie alive=False frais.
    """
    global _last_end_reconcile_at

    if not tiktool_configured() or _tiktool_is_rate_limited():
        return 0

    now = timezone.now()
    if (
        _last_end_reconcile_at is not None
        and (now - _last_end_reconcile_at).total_seconds() < max(min_interval_seconds, 45.0)
    ):
        return 0

    active = list(
        Live.objects.filter(statut=Live.STATUT_EN_COURS)
        .select_related('vendeur')
        .order_by('vendeur_id', '-date_live')
    )
    if not active:
        return 0

    _last_end_reconcile_at = now
    closed = 0
    seen_vendeurs: set[int] = set()

    for live in active:
        if live.vendeur_id in seen_vendeurs:
            continue
        if not _live_is_tiktok_tracked(live):
            titre = (live.titre or '').lower()
            if 'tiktok' not in titre:
                continue
        unique_id = resolve_vendeur_tiktok_unique_id(live.vendeur)
        if not unique_id:
            continue
        seen_vendeurs.add(live.vendeur_id)

        offline = confirm_streamer_is_offline(unique_id)
        if offline is True:
            n = cloturer_tiktok_lives_for_streamer(unique_id, reason='tiktok_offline_reconcile')
            closed += n
            logger.info(
                'Reconcile TikTok @%s : room_id offline confirmé → %s live(s) clôturé(s)',
                unique_id,
                n,
            )
        elif offline is False:
            logger.debug('Reconcile TikTok @%s : encore alive — on garde', unique_id)
        if _tiktool_is_rate_limited():
            break
    return closed


def sync_external_tiktok_lives(
    *,
    min_interval_seconds: float = 120.0,
    vendeur_id: int | None = None,
    rest: bool = True,
    wait_ws_seconds: float = 20.0,
) -> dict[str, int]:
    """Détecte un live TikTok pour les comptes **connectés** (OAuth).

    Ordre (économise le quota sandbox) :
    1. Démarrer/maintenir les scouts WebSocket
    2. Attendre un signal fort (chat/gift) ou roomInfo confirmé REST
    3. Sinon 1× POST `/webcast/room_id` si `rest=True`
    """
    global _last_tiktok_sync_at

    if not tiktool_configured():
        return {'started': 0, 'stopped': 0, 'skipped': 0}

    now = timezone.now()
    with _tiktok_sync_lock:
        if vendeur_id is not None:
            last_v = _last_vendeur_sync_at.get(vendeur_id)
            if last_v is not None and (now - last_v).total_seconds() < max(min_interval_seconds, 1.0):
                return {'started': 0, 'stopped': 0, 'skipped': 0, 'throttled': 1}
            _last_vendeur_sync_at[vendeur_id] = now
        else:
            if (
                _last_tiktok_sync_at is not None
                and (now - _last_tiktok_sync_at).total_seconds() < max(min_interval_seconds, 1.0)
            ):
                return {'started': 0, 'stopped': 0, 'skipped': 0, 'throttled': 1}
            _last_tiktok_sync_at = now

    started = 0
    stopped = 0
    skipped = 0

    try:
        if _tiktool_ws_is_rate_limited():
            logger.warning(
                'Sync TikTok : quota WebSocket horaire atteint (pause ~%.0fs). '
                'Attends le reset ou utilise REST si quota API > 0.',
                tiktool_ws_rate_limit_remaining_seconds(),
            )
            n_scouts = 0
        else:
            n_scouts = ensure_tiktok_scouts(vendeur_id=vendeur_id)
            logger.info('Sync TikTok : %s scout(s) WS actifs/démarrés', n_scouts)
    except Exception:
        logger.exception('ensure_tiktok_scouts a échoué')
        n_scouts = 0

    ws_ok = n_scouts > 0 and not _tiktool_ws_is_rate_limited()
    effective_wait = wait_ws_seconds if ws_ok else 0.0

    for vendeur, unique_id in iter_connected_tiktok_vendeurs(vendeur_id=vendeur_id):
        already = (
            Live.objects.filter(vendeur=vendeur, statut=Live.STATUT_EN_COURS)
            .order_by('-date_live')
            .first()
        )
        if already is None and effective_wait > 0:
            logger.info(
                'Sync TikTok @%s : attente signal WebSocket (%.0fs)…',
                unique_id,
                effective_wait,
            )
            deadline = time.time() + effective_wait
            while time.time() < deadline:
                time.sleep(0.5)
                # Si 4429 pendant l’attente, inutile de rester bloqué.
                if _tiktool_ws_is_rate_limited():
                    break
                already = (
                    Live.objects.filter(vendeur=vendeur, statut=Live.STATUT_EN_COURS)
                    .order_by('-date_live')
                    .first()
                )
                if already is not None:
                    break

        if already is not None:
            ensure_tiktok_confirmation_comment(already)
            ensure_tiktool_listener(already)
            started += 1
            continue

        is_live = None
        if rest and not _tiktool_is_rate_limited():
            is_live = check_streamer_is_live(unique_id, deep=True)

        if is_live is True:
            live = ensure_tiktok_live_for_streamer(unique_id, already_verified=True)
            if live:
                ensure_tiktok_confirmation_comment(live)
                ensure_tiktool_listener(live)
                started += 1
            continue

        if is_live is False:
            # Offline confirmé → clôturer / archiver les lives TikTok suivis.
            for live in Live.objects.filter(
                vendeur=vendeur, statut=Live.STATUT_EN_COURS
            ).order_by('-date_live'):
                if not _live_is_tiktok_tracked(live):
                    titre = (live.titre or '').lower()
                    if normalize_tiktok_username(unique_id) not in titre and 'tiktok' not in titre:
                        continue
                if cloturer_tiktok_live(live, reason='tiktok_offline_rest'):
                    stopped += 1
            continue

        logger.info(
            'Sync TikTok @%s : pas encore de preuve live (WS/REST) — vendeur #%s',
            unique_id,
            vendeur.pk,
        )
        skipped += 1

    result = {'started': started, 'stopped': stopped, 'skipped': skipped}
    if _tiktool_ws_is_rate_limited():
        result['ws_rate_limited'] = 1
    if _tiktool_is_rate_limited():
        result['rate_limited'] = 1
    return result


def recover_tiktool_listeners() -> int:
    """Relance détection TikTok + WS des lives encore en cours.

    Mode on_demand (défaut) :
      1) REST pour détecter un live
      2) WS uniquement pour les lives EN_COURS (capture JP)

    Mode always : scouts permanents + listeners.

    Si quota WS (4429) : REST seulement, aucune nouvelle connexion WS.
    """
    restarted = 0
    ws_paused = _tiktool_ws_is_rate_limited()

    if ws_paused:
        remaining = tiktool_ws_rate_limit_remaining_seconds()
        logger.warning(
            'TikTools WS en pause (reste ~%.0fs) — détection via REST (pas de nouvelle connexion WS)',
            remaining,
        )
        print(
            f'\n[TIKTOOL] Quota WS atteint (reste ~{remaining:.0f}s). '
            f'Détection live via REST en attendant…\n',
            flush=True,
        )

    # Toujours tenter la détection REST (surtout en on_demand / pause WS).
    if ws_paused or _ws_on_demand():
        try:
            result = sync_external_tiktok_lives(
                rest=True,
                wait_ws_seconds=0,
                min_interval_seconds=120.0,
            )
            restarted += int(result.get('started') or 0)
            if result.get('started'):
                logger.info(
                    'REST TikTok : %s live(s) démarré(s) (mode=%s, ws_paused=%s)',
                    result.get('started'),
                    _ws_mode(),
                    ws_paused,
                )
        except Exception:
            logger.exception('REST sync TikTok a échoué dans recover')

    if ws_paused:
        return restarted

    if _ws_on_demand():
        # Mode manuel : ne pas ouvrir de WS ici — le vendeur clique « Activer capture JP ».
        if _ws_manual_capture():
            with _listeners_lock:
                restarted += _ws_promote_pending_locked()
            status = tiktool_ws_pool_status()
            logger.info(
                'Pool WS status (manual): actifs=%s/%s file=%s',
                status['active'],
                status['max_connections'],
                status['pending'],
            )
            return restarted

        # WS seulement pour lives déjà EN_COURS.
        lives = Live.objects.filter(statut=Live.STATUT_EN_COURS).select_related('vendeur')
        for live in lives:
            if not live.vendeur.tiktok_username:
                continue
            if ensure_tiktool_listener(live):
                restarted += 1
        # Si des slots se sont libérés, promouvoir la file.
        with _listeners_lock:
            restarted += _ws_promote_pending_locked()
        status = tiktool_ws_pool_status()
        logger.info(
            'Pool WS status: actifs=%s/%s file=%s mode=%s',
            status['active'],
            status['max_connections'],
            status['pending'],
            status['mode'],
        )
        return restarted

    # Mode always : scouts permanents.
    try:
        restarted += ensure_tiktok_scouts()
    except Exception:
        logger.exception('recover: ensure_tiktok_scouts a échoué')

    lives = Live.objects.filter(statut=Live.STATUT_EN_COURS).select_related('vendeur')
    for live in lives:
        if not live.vendeur.tiktok_username:
            continue
        if ensure_tiktool_listener(live):
            restarted += 1
    with _listeners_lock:
        restarted += _ws_promote_pending_locked()
    return restarted
