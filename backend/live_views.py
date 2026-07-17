from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .live_service import LiveServiceError, arreter_live, demarrer_live
from .models import Live
from .serializers import LiveSerializer
from .tiktool_live import (
    capture_jp_status_for_live,
    start_capture_jp_for_live,
    stop_capture_jp_for_live,
)


def _live_for_response(live: Live) -> Live:
    """Recharge léger après démarrer/arrêter (IDs dressing, pas de N+1)."""
    return (
        Live.objects.select_related('vendeur', 'operateur')
        .prefetch_related('produits_dressing', 'codes_jp')
        .get(pk=live.pk)
    )


class LiveDemarrerAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, pk):
        live = get_object_or_404(Live.objects.select_related('vendeur'), pk=pk)
        try:
            live = demarrer_live(live)
            return Response(
                {
                    'detail': 'Live démarré sur toutes les plateformes connectées.',
                    'live': LiveSerializer(_live_for_response(live)).data,
                },
                status=status.HTTP_200_OK,
            )
        except LiveServiceError as exc:
            return Response({'detail': exc.message, **exc.payload}, status=exc.status_code)


class LiveArreterAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, pk):
        live = get_object_or_404(Live.objects.select_related('vendeur'), pk=pk)
        # Libère le slot WS capture JP si actif.
        try:
            stop_capture_jp_for_live(live)
        except Exception:
            pass
        live = arreter_live(live)
        return Response(
            {
                'detail': 'Live arrêté sur toutes les plateformes.',
                'live': LiveSerializer(_live_for_response(live)).data,
            },
            status=status.HTTP_200_OK,
        )


class LiveCaptureJpStatusAPIView(APIView):
    """GET état capture JP (WS) pour un live."""

    permission_classes = [AllowAny]

    def get(self, request, pk):
        live = get_object_or_404(Live.objects.select_related('vendeur'), pk=pk)
        return Response(capture_jp_status_for_live(live), status=status.HTTP_200_OK)


class LiveCaptureJpStartAPIView(APIView):
    """POST — bouton vendeur « Activer capture JP »."""

    permission_classes = [AllowAny]

    def post(self, request, pk):
        live = get_object_or_404(Live.objects.select_related('vendeur'), pk=pk)
        result = start_capture_jp_for_live(live)
        code = status.HTTP_200_OK if result.get('ok') else status.HTTP_400_BAD_REQUEST
        if result.get('status', {}).get('queued'):
            code = status.HTTP_200_OK
        return Response(result, status=code)


class LiveCaptureJpStopAPIView(APIView):
    """POST — bouton vendeur « Arrêter capture JP »."""

    permission_classes = [AllowAny]

    def post(self, request, pk):
        live = get_object_or_404(Live.objects.select_related('vendeur'), pk=pk)
        result = stop_capture_jp_for_live(live)
        return Response(result, status=status.HTTP_200_OK)
