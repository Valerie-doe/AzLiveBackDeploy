"""Routes de l'intégration LLM (montées sous /api/llm/)."""
from django.urls import path

from .views import (
    LLMAnalyzeCommentAPIView,
    LLMAnalyzeConfirmationAPIView,
    LLMChatAPIView,
    LLMHealthAPIView,
)

urlpatterns = [
    path('health/', LLMHealthAPIView.as_view(), name='llm-health'),
    path('chat/', LLMChatAPIView.as_view(), name='llm-chat'),
    path('analyze-comment/', LLMAnalyzeCommentAPIView.as_view(), name='llm-analyze-comment'),
    path('analyze-confirmation/', LLMAnalyzeConfirmationAPIView.as_view(), name='llm-analyze-confirmation'),
]
