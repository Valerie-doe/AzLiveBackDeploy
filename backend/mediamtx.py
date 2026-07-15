"""Pont WebRTC (navigateur) -> RTMPS (Facebook Live) via MediaMTX.

Le navigateur du vendeur publie sa webcam en WHIP vers MediaMTX. Pour chaque live,
Django crée dynamiquement un "path" MediaMTX dont le hook runOnReady lance ffmpeg :
ce dernier relit le flux (RTSP local) et le relaie vers le secure_stream_url de Facebook.

La clé RTMP Facebook reste ainsi côté serveur : le navigateur ne reçoit qu'une URL WHIP
et un token de publication temporaire.
"""
import json
import logging
import secrets
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)


class MediaMTXError(Exception):
    def __init__(self, message, status_code=502):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def mediamtx_enabled() -> bool:
    return bool(getattr(settings, 'MEDIAMTX_ENABLED', False))


def _api_request(path: str, method: str = 'GET', payload: dict | None = None) -> Any:
    base = settings.MEDIAMTX_API_URL.rstrip('/')
    url = f'{base}/{path.lstrip("/")}'
    data = None
    headers = {'User-Agent': 'AZLive/1.0'}
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'

    request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read().decode('utf-8')
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        try:
            message = exc.read().decode('utf-8') or str(exc)
        except Exception:  # noqa: BLE001
            message = str(exc)
        raise MediaMTXError(f'MediaMTX API {exc.code}: {message}', status_code=502) from exc
    except urllib.error.URLError as exc:
        raise MediaMTXError(f'MediaMTX injoignable: {exc.reason}', status_code=503) from exc


def _build_relay_command(secure_stream_url: str) -> str:
    """Commande ffmpeg lancée par MediaMTX (runOnReady) pour relayer vers Facebook.

    $MTX_PATH est remplacé par MediaMTX au moment de l'exécution. Facebook exige
    H.264 + AAC, on transcode donc systématiquement.
    """
    rtsp_host = settings.MEDIAMTX_RTSP_HOST
    bitrate = settings.MEDIAMTX_FFMPEG_VIDEO_BITRATE
    preset = settings.MEDIAMTX_FFMPEG_PRESET
    # Le secure_stream_url est entre guillemets simples pour neutraliser ? et & de la query.
    safe_url = secure_stream_url.replace("'", "")
    return (
        f'ffmpeg -hide_banner -loglevel warning '
        f'-rtsp_transport tcp -i rtsp://{rtsp_host}/$MTX_PATH '
        f'-c:v libx264 -preset {preset} -pix_fmt yuv420p '
        f'-b:v {bitrate} -maxrate {bitrate} -bufsize {bitrate} -g 60 '
        f'-c:a aac -b:a 128k -ar 44100 '
        f"-f flv '{safe_url}'"
    )


def provision_live_path(live, secure_stream_url: str) -> dict[str, Any]:
    """Crée un path MediaMTX dédié au live et renvoie les infos pour le navigateur.

    Retourne : {path, whip_url, publish_token}.
    """
    if not secure_stream_url:
        raise MediaMTXError("Aucune URL de diffusion Facebook (secure_stream_url) disponible.", status_code=400)

    path = f'live_{live.pk}_{secrets.token_hex(6)}'
    publish_token = secrets.token_urlsafe(24)

    _api_request(
        f'/v3/config/paths/add/{urllib.parse.quote(path)}',
        method='POST',
        payload={
            'source': 'publisher',
            'runOnReady': _build_relay_command(secure_stream_url),
            'runOnReadyRestart': True,
        },
    )

    whip_base = settings.MEDIAMTX_WHIP_BASE_URL.rstrip('/')
    # Token aussi en query : plus fiable que Authorization Bearer
    # (MediaMTX → authHTTP vers Django sur Railway).
    whip_url = (
        f'{whip_base}/{path}/whip'
        f'?token={urllib.parse.quote(publish_token, safe="")}'
    )
    return {
        'path': path,
        'whip_url': whip_url,
        'publish_token': publish_token,
    }


def teardown_live_path(path: str) -> None:
    """Supprime le path MediaMTX (arrête le relais ffmpeg). Best-effort."""
    if not path:
        return
    try:
        _api_request(f'/v3/config/paths/delete/{urllib.parse.quote(path)}', method='DELETE')
    except MediaMTXError as exc:
        logger.warning('Suppression du path MediaMTX %s échouée: %s', path, exc.message)
