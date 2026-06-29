import json

from django.conf import settings
from django.http import HttpResponse
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .facebook_oauth import facebook_configured
from .facebook_webhooks import (
    process_facebook_webhook_payload,
    verify_webhook_signature,
)
from .jp_capture import JPCaptureError, process_social_comment
from .order_confirmation import OrderConfirmationError, process_inbound_private_message
from .tiktool_live import process_tiktool_chat_event
from .ai import JPCommentAnalyzer


class FacebookWebhookView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        mode = request.query_params.get('hub.mode')
        token = request.query_params.get('hub.verify_token')
        challenge = request.query_params.get('hub.challenge')

        if mode == 'subscribe' and token == settings.FACEBOOK_WEBHOOK_VERIFY_TOKEN:
            return HttpResponse(challenge, content_type='text/plain')

        return Response({'detail': 'Token de vérification invalide.'}, status=status.HTTP_403_FORBIDDEN)

    def post(self, request):
        raw_body = request.body
        signature = request.META.get('HTTP_X_HUB_SIGNATURE_256')

        if facebook_configured() and not verify_webhook_signature(raw_body, signature):
            return Response({'detail': 'Signature webhook invalide.'}, status=status.HTTP_403_FORBIDDEN)

        try:
            payload = json.loads(raw_body.decode('utf-8') or '{}')
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = request.data

        outcome = process_facebook_webhook_payload(payload)
        return Response(
            {
                'processed': len(outcome['results']),
                'results': outcome['results'],
            },
            status=outcome['status_code'],
        )


class TikTokWebhookView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        event = request.data.get('event')
        if event == 'chat':
            streamer_unique_id = (
                request.data.get('uniqueId')
                or request.data.get('streamer_unique_id')
                or (request.data.get('data') or {}).get('uniqueId')
            )
            event_data = request.data.get('data') or {}
            if not streamer_unique_id:
                return Response(
                    {'detail': 'uniqueId ou streamer_unique_id requis pour un event TikTools chat.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                result = process_tiktool_chat_event(str(streamer_unique_id), event_data)
                status_code = 201 if result.get('status') != 'ignored' else status.HTTP_200_OK
                return Response(result, status=status_code)
            except JPCaptureError as exc:
                return Response({'error': exc.message, **exc.payload}, status=exc.status_code)

        sender_tiktok_id = request.data.get('sender_tiktok_id')
        sender_name = request.data.get('sender_name', 'Client TikTok')
        comment_text = request.data.get('comment_text') or request.data.get('message_text', '')
        streamer_unique_id = request.data.get('streamer_unique_id')

        if not sender_tiktok_id or not comment_text:
            return Response(
                {'detail': 'sender_tiktok_id et comment_text (ou message_text) requis.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        analysis = JPCommentAnalyzer().analyze(comment_text)
        if analysis.get('intent') != 'achat':
            try:
                result = process_inbound_private_message(
                    sender_id=str(sender_tiktok_id),
                    message_text=comment_text,
                    channel='TikTok',
                    id_field='tiktok_id',
                )
                return Response(result, status=status.HTTP_200_OK)
            except OrderConfirmationError as exc:
                return Response({'detail': exc.message, **exc.payload}, status=exc.status_code)

        try:
            result = process_social_comment(
                sender_id=str(sender_tiktok_id),
                sender_name=sender_name,
                comment_text=comment_text,
                channel='TikTok',
                streamer_unique_id=streamer_unique_id,
                id_field='tiktok_id',
            )
            status_code = 201 if result.get('status') != 'ignored' else status.HTTP_200_OK
            return Response(result, status=status_code)
        except JPCaptureError as exc:
            return Response({'error': exc.message, **exc.payload}, status=exc.status_code)
