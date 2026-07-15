"""MediaMTX : auth HTTP + proxy WHIP (contournement Railway 502 sur le domaine MediaMTX).

Sur Railway, le domaine public MediaMTX renvoie souvent 502 alors que l'API privée :9997
fonctionne. Le navigateur passe alors par Django (HTTPS déjà OK) qui relaie le WHIP
vers MediaMTX en private networking.
"""
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.http import HttpResponse
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Live

logger = logging.getLogger(__name__)

# Actions autorisées sans token (lecture interne par ffmpeg, contrôle/metrics).
_OPEN_ACTIONS = {'read', 'playback', 'api', 'metrics', 'pprof'}


class MediaMTXAuthAPIView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        try:
            payload = request.data if isinstance(request.data, dict) else json.loads(request.body or '{}')
        except (json.JSONDecodeError, ValueError):
            payload = {}

        action = (payload.get('action') or '').lower()
        path = payload.get('path') or ''

        if action in _OPEN_ACTIONS:
            return Response(status=status.HTTP_200_OK)

        if action == 'publish':
            token = (
                payload.get('token')
                or payload.get('password')
                or payload.get('user')
                or self._token_from_query(payload.get('query'))
                or self._bearer_from_request(request)
            )
            if self._publish_allowed(path, token):
                return Response(status=status.HTTP_200_OK)

        logger.warning(
            'MediaMTX auth refusé (action=%s, path=%s, protocol=%s)',
            action,
            path,
            payload.get('protocol'),
        )
        return Response({'detail': 'unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)

    @staticmethod
    def _bearer_from_request(request) -> str | None:
        auth = request.META.get('HTTP_AUTHORIZATION') or ''
        if auth.lower().startswith('bearer '):
            return auth[7:].strip() or None
        return None

    @staticmethod
    def _token_from_query(query: str | None) -> str | None:
        if not query:
            return None
        params = urllib.parse.parse_qs(query)
        for key in ('token', 'pass', 'password'):
            if params.get(key):
                return params[key][0]
        return None

    @staticmethod
    def _publish_allowed(path: str, token: str | None) -> bool:
        if not path or not token:
            return False
        live = (
            Live.objects.filter(statut=Live.STATUT_EN_COURS)
            .filter(diffusion_plateformes__webrtc__path=path)
            .first()
        )
        if live is None:
            for candidate in Live.objects.filter(statut=Live.STATUT_EN_COURS).order_by('-id')[:30]:
                webrtc = (candidate.diffusion_plateformes or {}).get('webrtc') or {}
                if webrtc.get('path') == path:
                    live = candidate
                    break
        if not live:
            logger.warning('MediaMTX auth: aucun live en_cours pour path=%s', path)
            return False
        expected = (live.diffusion_plateformes or {}).get('webrtc', {}).get('publish_token')
        ok = bool(expected) and token == expected
        if not ok:
            logger.warning('MediaMTX auth: token mismatch path=%s live=%s', path, live.pk)
        return ok


def _whip_internal_base() -> str:
    explicit = (getattr(settings, 'MEDIAMTX_WHIP_INTERNAL_BASE', '') or '').rstrip('/')
    if explicit:
        return explicit
    api = (getattr(settings, 'MEDIAMTX_API_URL', '') or '').rstrip('/')
    if not api:
        return ''
    parsed = urllib.parse.urlparse(api)
    if not parsed.hostname:
        return ''
    port = getattr(settings, 'MEDIAMTX_WEBRTC_PORT', None) or '8889'
    scheme = parsed.scheme or 'http'
    return f'{scheme}://{parsed.hostname}:{port}'


class MediaMTXWhipProxyAPIView(APIView):
    """Proxy WHIP : navigateur → Django (HTTPS public) → MediaMTX (réseau privé)."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def options(self, request, path: str):
        response = HttpResponse(status=204)
        response['Access-Control-Allow-Origin'] = '*'
        response['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        response['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        response['Access-Control-Max-Age'] = '86400'
        return response

    def post(self, request, path: str):
        base = _whip_internal_base()
        if not base:
            return Response(
                {
                    'detail': (
                        'MEDIAMTX_WHIP_INTERNAL_BASE / MEDIAMTX_API_URL non configuré '
                        'pour le proxy WHIP.'
                    )
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        path = path.strip('/')
        token = request.query_params.get('token') or ''
        auth = request.META.get('HTTP_AUTHORIZATION') or ''
        if not token and auth.lower().startswith('bearer '):
            token = auth[7:].strip()

        # Sécurité : le token est validé ICI (MediaMTX n'appelle plus authHTTP
        # publique Railway, qui timeoute en hairpin).
        if not MediaMTXAuthAPIView._publish_allowed(path, token or None):
            return Response(
                {'detail': 'Token WHIP invalide ou live non démarré.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        target = f'{base}/{path}/whip'
        body = request.body or b''
        headers = {
            'Content-Type': request.META.get('CONTENT_TYPE') or 'application/sdp',
            'User-Agent': 'AZLive-WHIP-Proxy/1.0',
        }

        req = urllib.request.Request(target, data=body, headers=headers, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=45) as upstream:
                answer = upstream.read()
                content_type = upstream.headers.get('Content-Type', 'application/sdp')
                response = HttpResponse(answer, status=upstream.status, content_type=content_type)
        except urllib.error.HTTPError as exc:
            detail = exc.read()
            logger.warning(
                'WHIP proxy HTTP %s vers %s: %s',
                exc.code,
                target,
                detail[:300],
            )
            response = HttpResponse(detail, status=exc.code, content_type='text/plain')
        except urllib.error.URLError as exc:
            logger.exception('WHIP proxy injoignable (%s): %s', target, exc.reason)
            return Response(
                {
                    'detail': (
                        f'MediaMTX WHIP interne injoignable ({exc.reason}). '
                        'Vérifie MEDIAMTX_WHIP_INTERNAL_BASE '
                        '(ex. http://azlivemtxn.railway.internal:8189) = port WHIP des logs MediaMTX.'
                    )
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        response['Access-Control-Allow-Origin'] = '*'
        return response
