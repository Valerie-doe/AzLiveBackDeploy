"""Endpoints DRF de l'intégration LLM (test & utilisation).

Tous les endpoints sont en AllowAny (cohérent avec les autres routes MVP du
projet) pour faciliter les tests Postman.
"""
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from . import client as llm_client
from . import config, service


class LLMHealthAPIView(APIView):
    """GET — état de la configuration LLM (clé présente, modèle, etc.)."""
    permission_classes = [AllowAny]

    def get(self, request):
        return Response(config.llm_status())


class LLMChatAPIView(APIView):
    """POST — appel direct au LLM (test brut). Body: {"prompt": "...", "json_mode": false}."""
    permission_classes = [AllowAny]

    def post(self, request):
        prompt = (request.data.get('prompt') or '').strip()
        if not prompt:
            return Response({'detail': 'Champ "prompt" requis.'}, status=400)

        json_mode = bool(request.data.get('json_mode', False))
        try:
            content = (
                llm_client.generate_json(prompt)
                if json_mode
                else llm_client.generate(prompt, json_mode=False)
            )
            return Response({
                'provider': 'google-gemini',
                'model': config.GEMINI_MODEL,
                'json_mode': json_mode,
                'content': content,
            })
        except llm_client.LLMError as exc:
            return Response({'detail': exc.message, **exc.payload}, status=exc.status_code)


class LLMAnalyzeCommentAPIView(APIView):
    """POST — analyse un commentaire de live.

    Body: {"comment_text": "...", "live_id": optionnel, "vendeur_id": optionnel}.
    `live_id`/`vendeur_id` restreignent le catalogue fourni au LLM (meilleur matching).
    """
    permission_classes = [AllowAny]

    def post(self, request):
        text = (request.data.get('comment_text') or '').strip()
        if not text:
            return Response({'detail': 'Champ "comment_text" requis.'}, status=400)
        return Response(service.analyze_comment(
            text,
            vendeur_id=request.data.get('vendeur_id'),
            live_id=request.data.get('live_id'),
        ))


class LLMAnalyzeConfirmationAPIView(APIView):
    """POST — analyse une réponse privée client.

    Body: {"message_text": "...", "client_context": {optionnel}}.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        text = (request.data.get('message_text') or '').strip()
        if not text:
            return Response({'detail': 'Champ "message_text" requis.'}, status=400)
        client_context = request.data.get('client_context') or None
        return Response(service.analyze_confirmation(text, client_context=client_context))
