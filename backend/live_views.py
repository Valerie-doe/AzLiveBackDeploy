from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .live_service import LiveServiceError, arreter_live, demarrer_live
from .models import Live
from .serializers import LiveSerializer


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
        live = arreter_live(live)
        return Response(
            {
                'detail': 'Live arrêté sur toutes les plateformes.',
                'live': LiveSerializer(_live_for_response(live)).data,
            },
            status=status.HTTP_200_OK,
        )
