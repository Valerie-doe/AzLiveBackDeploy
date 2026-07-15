"""Endpoint d'authentification appelé par MediaMTX (authHTTPAddress).

MediaMTX appelle cette vue avant chaque action (publish/read...). On autorise :
- les lectures internes (ffmpeg relit le flux en RTSP local) ;
- les publications WHIP dont le token correspond à celui provisionné pour le live.

Réponse : 200 = autorisé, 401 = refusé (convention MediaMTX).
"""
import json
import logging

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
        from urllib.parse import parse_qs

        params = parse_qs(query)
        for key in ('token', 'pass', 'password'):
            if params.get(key):
                return params[key][0]
        return None

    @staticmethod
    def _publish_allowed(path: str, token: str | None) -> bool:
        if not path or not token:
            return False
        # Lookup JSON Field (Postgres) + fallback Python pour robustesse.
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
