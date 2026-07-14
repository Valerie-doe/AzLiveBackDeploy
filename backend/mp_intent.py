from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

MP_INTENTS = frozenset({
    'annulation',
    'annulation_modification',
    'modification',
    'infos_commande',
    'remerciement',
    'reprise',
    'question',
    'autre',
})

MP_INTENT_LEXICON = (
    'annulation : annuler la COMMANDE entière, foana, esory, tsy alaiko, tsy ilaiko, '
    'je ne veux plus la commande ; '
    'annulation_modification : annuler seulement la DERNIÈRE modification, ajanony ny fanovana, '
    'tsy alefa ny fanovana, remettre comme avant ; '
    'modification : hanova, ovaina, changer adresse/numéro/date/qté ; '
    'infos_commande : donne nom, téléphone, adresse, date, heure, quantité pour confirmer ; '
    'remerciement : misaotra, mankasitraka, merci ; '
    'reprise : mbola te-hividy, reprendre après annulation ; '
    'question : prix, stock, lieu, disponibilité, question sans fiche infos.'
)

MP_INTENT_SYSTEM_PROMPT = (
    "Tu analyses un message privé Messenger d'un client d'un live shopping à Madagascar. "
    "Langue : malgache, français, mélange, fautes courantes. "
    "Renvoie UNIQUEMENT un objet JSON valide avec :\n"
    '- "intent": une parmi "annulation", "annulation_modification", "modification", "infos_commande", '
    '"remerciement", "reprise", "question", "autre".\n'
    '- "confiance": nombre entre 0 et 1.\n'
    "Règles :\n"
    "- annulation si le client ne veut plus la commande entière.\n"
    "- annulation_modification si il veut annuler une modification récente sans annuler la commande.\n"
    "- modification seulement si il veut CHANGER une commande déjà faite (hanova, ovaina…).\n"
    "- infos_commande si il donne (ou complète) nom, tél, adresse, date, heure, quantité.\n"
    "- Ne confonds pas annulation et modification.\n"
    "Glossaire : " + MP_INTENT_LEXICON
)


class LLMPrivateMessageIntentAnalyzer:
    """Classifie l'intention d'un MP via LLM (JSON strict)."""

    def __init__(self):
        self.base_url = getattr(settings, 'AZLIVE_LLM_BASE_URL', '').rstrip('/')
        self.api_key = getattr(settings, 'AZLIVE_LLM_API_KEY', '')
        self.model = getattr(settings, 'AZLIVE_LLM_MODEL', '')
        self.timeout = getattr(settings, 'AZLIVE_LLM_TIMEOUT', 12)
        self.enabled = bool(
            getattr(settings, 'AZLIVE_LLM_ENABLED', False)
            and self.base_url
            and self.model
        )

    def is_enabled(self) -> bool:
        return self.enabled

    def classify(self, text: str) -> dict[str, Any] | None:
        if not self.enabled or not (text or '').strip():
            return None
        try:
            raw = self._call_llm(text)
        except (urllib.error.URLError, TimeoutError) as exc:
            logger.warning('LLM intent MP indisponible: %s', exc)
            return None
        except Exception:  # noqa: BLE001
            logger.exception('Erreur LLM intent MP')
            return None
        fields = self._parse_json(raw)
        if not fields:
            return None
        intent = str(fields.get('intent') or 'autre').lower().strip()
        if intent not in MP_INTENTS:
            intent = 'autre'
        confiance = self._clean_float(fields.get('confiance')) or 0.5
        return {
            'intent': intent,
            'confiance': confiance,
            'source': 'llm',
        }

    def _call_llm(self, text: str) -> str:
        payload = {
            'model': self.model,
            'messages': [
                {'role': 'system', 'content': MP_INTENT_SYSTEM_PROMPT},
                {'role': 'user', 'content': text.strip()},
            ],
            'temperature': 0,
            'response_format': {'type': 'json_object'},
        }
        data = json.dumps(payload).encode('utf-8')
        request = urllib.request.Request(
            f'{self.base_url}/chat/completions',
            data=data,
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {self.api_key}',
                'User-Agent': 'AZLive/1.0',
            },
            method='POST',
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = json.loads(response.read().decode('utf-8'))
        return body['choices'][0]['message']['content']

    @staticmethod
    def _parse_json(raw: str) -> dict | None:
        if not raw:
            return None
        text = raw.strip()
        if text.startswith('```'):
            text = text.strip('`')
            if text.lower().startswith('json'):
                text = text[4:]
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            start, end = text.find('{'), text.rfind('}')
            if start == -1 or end == -1 or end <= start:
                return None
            try:
                parsed = json.loads(text[start : end + 1])
            except (json.JSONDecodeError, ValueError):
                return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _clean_float(value) -> float | None:
        try:
            score = float(value)
        except (TypeError, ValueError):
            return None
        return max(0.0, min(1.0, score))


def _message_looks_like_order_info(text: str, parsed: dict[str, str]) -> bool:
    from .human_assistance import OFF_TOPIC_HINTS
    from .order_confirmation import (
        _is_quantity_line,
        _looks_like_date,
        _looks_like_phone,
        _looks_like_time,
        _parsed_has_updatable_fields,
    )

    cleaned = (text or '').strip()
    if OFF_TOPIC_HINTS.search(cleaned) or '?' in cleaned:
        return False
    if _parsed_has_updatable_fields(parsed):
        return True
    if _looks_like_phone(cleaned) or _looks_like_date(cleaned) or _looks_like_time(cleaned):
        return True
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if len(lines) >= 2:
        return True
    if len(lines) == 1 and _is_quantity_line(lines[0]):
        return True
    return False


def classify_mp_intent_regex(text: str, client=None) -> dict[str, Any] | None:
    """Intentions claires via regex — None si ambigu."""
    from .human_assistance import OFF_TOPIC_HINTS
    from .order_confirmation import (
        _is_cancellation,
        _is_modification_cancellation,
        _looks_like_modification,
        _looks_like_reprise,
        _looks_like_thanks,
        analyze_confirmation_message,
        parse_confirmation_text,
    )

    cleaned = (text or '').strip()
    if not cleaned:
        return None

    if _looks_like_thanks(cleaned):
        return {'intent': 'remerciement', 'confiance': 0.95, 'source': 'regex'}
    if _is_modification_cancellation(cleaned):
        return {'intent': 'annulation_modification', 'confiance': 0.96, 'source': 'regex'}
    if _is_cancellation(cleaned):
        return {'intent': 'annulation', 'confiance': 0.95, 'source': 'regex'}
    if _looks_like_reprise(cleaned):
        return {'intent': 'reprise', 'confiance': 0.92, 'source': 'regex'}
    if _looks_like_modification(cleaned):
        return {'intent': 'modification', 'confiance': 0.92, 'source': 'regex'}
    if OFF_TOPIC_HINTS.search(cleaned) or '?' in cleaned:
        return {'intent': 'question', 'confiance': 0.85, 'source': 'regex'}

    parsed = analyze_confirmation_message(cleaned, client=client) if client else parse_confirmation_text(cleaned)
    if _message_looks_like_order_info(cleaned, parsed):
        return {'intent': 'infos_commande', 'confiance': 0.88, 'source': 'regex'}

    return None


class HybridPrivateMessageIntentClassifier:
    """Regex d'abord, LLM si ambigu (même config AZLIVE_LLM_*)."""

    REGEX_MIN_CONFIDENCE = 0.8
    LLM_MIN_CONFIDENCE = 0.55

    def __init__(self):
        self.llm = LLMPrivateMessageIntentAnalyzer()

    def classify(self, text: str, client=None) -> dict[str, Any]:
        regex_result = classify_mp_intent_regex(text, client=client)
        if regex_result and regex_result.get('confiance', 0) >= self.REGEX_MIN_CONFIDENCE:
            return regex_result

        if self.llm.is_enabled():
            llm_result = self.llm.classify(text)
            if llm_result and llm_result.get('confiance', 0) >= self.LLM_MIN_CONFIDENCE:
                return llm_result

        if regex_result:
            return regex_result

        return {
            'intent': 'autre',
            'confiance': 0.25,
            'source': 'fallback',
        }


def classify_private_message_intent(text: str, client=None) -> dict[str, Any]:
    return HybridPrivateMessageIntentClassifier().classify(text, client=client)
