import urllib.parse

from django.conf import settings
from django.http import HttpResponseRedirect
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .facebook_oauth import (
    FacebookOAuthError,
    authenticate_with_access_token,
    authenticate_with_code,
    build_oauth_url,
    facebook_configured,
    generate_oauth_state,
    sync_vendeur_pages,
)
from .facebook_webhooks import subscribe_vendeur_pages
from .models import Vendeur
from .serializers import VendeurSerializer
from .tiktok_oauth import (
    TikTokOAuthError,
    authenticate_with_access_token as tiktok_authenticate_with_access_token,
    authenticate_with_code as tiktok_authenticate_with_code,
    build_oauth_url as build_tiktok_oauth_url,
    generate_oauth_state as generate_tiktok_oauth_state,
    tiktok_configured,
)


def _auth_payload(vendeur, user, created, token):
    return {
        'token': token,
        'created': created,
        'user': {
            'id': user.id,
            'username': user.username,
            'email': user.email,
        },
        'vendeur': VendeurSerializer(vendeur).data,
    }


def _respond_or_redirect(request, payload, success_url_setting):
    if request.query_params.get('format') == 'json' or 'application/json' in request.headers.get('Accept', ''):
        return Response(payload, status=status.HTTP_200_OK)

    redirect_base = success_url_setting.rstrip('/')
    query = urllib.parse.urlencode({'token': payload['token'], 'created': str(payload['created']).lower()})
    return HttpResponseRedirect(f'{redirect_base}?{query}')


class FacebookLoginURLAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        if not facebook_configured():
            return Response(
                {'detail': 'Facebook OAuth n\'est pas configuré (FACEBOOK_APP_ID / FACEBOOK_APP_SECRET).'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        state = generate_oauth_state()
        return Response(
            {
                'auth_url': build_oauth_url(state),
                'state': state,
                'redirect_uri': settings.FACEBOOK_REDIRECT_URI,
                'scopes': settings.FACEBOOK_OAUTH_SCOPES.split(','),
            },
            status=status.HTTP_200_OK,
        )


class FacebookCallbackAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        if not facebook_configured():
            return Response(
                {'detail': 'Facebook OAuth n\'est pas configuré.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        error = request.query_params.get('error')
        if error:
            description = request.query_params.get('error_description', error)
            return Response({'detail': description}, status=status.HTTP_400_BAD_REQUEST)

        code = request.query_params.get('code')
        state = request.query_params.get('state')
        if not code:
            return Response({'detail': 'Le paramètre code est requis.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            vendeur, user, created, token = authenticate_with_code(code, state)
            payload = _auth_payload(vendeur, user, created, token)
            return _respond_or_redirect(request, payload, settings.FACEBOOK_LOGIN_SUCCESS_URL)
        except FacebookOAuthError as exc:
            return Response({'detail': exc.message}, status=exc.status_code)


class FacebookTokenLoginAPIView(APIView):
    """Connexion via access_token obtenu côté client (Facebook JS SDK / mobile)."""
    permission_classes = [AllowAny]

    def post(self, request):
        if not facebook_configured():
            return Response(
                {'detail': 'Facebook OAuth n\'est pas configuré.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        access_token = request.data.get('access_token')
        if not access_token:
            return Response({'detail': 'Le champ access_token est requis.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            vendeur, user, created, token = authenticate_with_access_token(access_token)
            return Response(_auth_payload(vendeur, user, created, token), status=status.HTTP_200_OK)
        except FacebookOAuthError as exc:
            return Response({'detail': exc.message}, status=exc.status_code)


class FacebookSyncPagesAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            vendeur = request.user.vendeur
        except Vendeur.DoesNotExist:
            return Response({'detail': 'Aucun profil vendeur lié à ce compte.'}, status=status.HTTP_404_NOT_FOUND)

        if not vendeur.facebook_access_token:
            return Response(
                {'detail': 'Connectez-vous d\'abord via Facebook pour synchroniser vos pages.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            pages = sync_vendeur_pages(vendeur)
            return Response(
                {
                    'vendeur': VendeurSerializer(vendeur).data,
                    'pages_synced': len(pages),
                },
                status=status.HTTP_200_OK,
            )
        except FacebookOAuthError as exc:
            return Response({'detail': exc.message}, status=exc.status_code)


class FacebookSubscribeWebhooksAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            vendeur = request.user.vendeur
        except Vendeur.DoesNotExist:
            return Response({'detail': 'Aucun profil vendeur lié à ce compte.'}, status=status.HTTP_404_NOT_FOUND)

        if not facebook_configured():
            return Response({'detail': 'Facebook OAuth n\'est pas configuré.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        try:
            pages = subscribe_vendeur_pages(vendeur)
            return Response(
                {
                    'subscribed_pages': pages,
                    'webhook_url': request.build_absolute_uri('/api/webhooks/facebook/'),
                    'verify_token': settings.FACEBOOK_WEBHOOK_VERIFY_TOKEN,
                    'subscribed_fields': settings.FACEBOOK_WEBHOOK_FIELDS.split(','),
                },
                status=status.HTTP_200_OK,
            )
        except FacebookOAuthError as exc:
            return Response({'detail': exc.message}, status=exc.status_code)


class AuthMeAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            vendeur = request.user.vendeur
        except Vendeur.DoesNotExist:
            vendeur = None

        return Response(
            {
                'user': {
                    'id': request.user.id,
                    'username': request.user.username,
                    'email': request.user.email,
                },
                'vendeur': VendeurSerializer(vendeur).data if vendeur else None,
                'facebook_connected': bool(vendeur and vendeur.facebook_user_id),
                'tiktok_connected': bool(vendeur and vendeur.tiktok_open_id),
            },
            status=status.HTTP_200_OK,
        )


class TikTokLoginURLAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        if not tiktok_configured():
            return Response(
                {'detail': 'TikTok OAuth n\'est pas configuré (TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET).'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        state, code_challenge = generate_tiktok_oauth_state()
        return Response(
            {
                'auth_url': build_tiktok_oauth_url(state, code_challenge),
                'state': state,
                'redirect_uri': settings.TIKTOK_REDIRECT_URI,
                'scopes': settings.TIKTOK_OAUTH_SCOPES.split(','),
            },
            status=status.HTTP_200_OK,
        )


class TikTokCallbackAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        if not tiktok_configured():
            return Response({'detail': 'TikTok OAuth n\'est pas configuré.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        error = request.query_params.get('error')
        if error:
            description = request.query_params.get('error_description', error)
            return Response({'detail': description}, status=status.HTTP_400_BAD_REQUEST)

        code = request.query_params.get('code')
        state = request.query_params.get('state')
        if not code:
            return Response({'detail': 'Le paramètre code est requis.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            vendeur, user, created, token = tiktok_authenticate_with_code(code, state)
            payload = _auth_payload(vendeur, user, created, token)
            return _respond_or_redirect(request, payload, settings.TIKTOK_LOGIN_SUCCESS_URL)
        except TikTokOAuthError as exc:
            return Response({'detail': exc.message}, status=exc.status_code)


class TikTokTokenLoginAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        if not tiktok_configured():
            return Response({'detail': 'TikTok OAuth n\'est pas configuré.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        access_token = request.data.get('access_token')
        if not access_token:
            return Response({'detail': 'Le champ access_token est requis.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            vendeur, user, created, token = tiktok_authenticate_with_access_token(access_token)
            return Response(_auth_payload(vendeur, user, created, token), status=status.HTTP_200_OK)
        except TikTokOAuthError as exc:
            return Response({'detail': exc.message}, status=exc.status_code)
