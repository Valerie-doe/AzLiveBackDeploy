"""Client minimal pour l'API REST Google Gemini (sans dépendance externe).

Utilise uniquement la bibliothèque standard (`urllib`) afin de ne pas ajouter
de paquet pip au projet. Fournit `generate()` (texte) et `generate_json()`
(parsing JSON robuste).
"""
import json
import logging
import re
import urllib.error
import urllib.request

from . import config

logger = logging.getLogger(__name__)


class LLMError(Exception):
    def __init__(self, message, status_code=502, payload=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload or {}


def is_enabled() -> bool:
    return bool(config.LLM_ENABLED and config.GEMINI_API_KEY)


def _endpoint() -> str:
    base = config.GEMINI_API_BASE.rstrip('/')
    return f'{base}/models/{config.GEMINI_MODEL}:generateContent?key={config.GEMINI_API_KEY}'


def generate(prompt: str, *, json_mode: bool = True, temperature: float = 0.0, system: str | None = None) -> str:
    """Appelle Gemini et renvoie le texte brut de la réponse."""
    if not is_enabled():
        raise LLMError(
            'LLM désactivé ou clé API manquante (définir GEMINI_API_KEY).',
            status_code=503,
        )

    parts = []
    if system:
        parts.append({'text': system})
    parts.append({'text': prompt})

    body = {
        'contents': [{'role': 'user', 'parts': parts}],
        'generationConfig': {'temperature': temperature},
    }
    if json_mode:
        body['generationConfig']['responseMimeType'] = 'application/json'

    request = urllib.request.Request(
        _endpoint(),
        data=json.dumps(body).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )

    try:
        with urllib.request.urlopen(request, timeout=config.LLM_TIMEOUT) as response:
            raw = response.read().decode('utf-8')
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace') if exc.fp else str(exc)
        logger.warning('Gemini HTTP %s: %s', exc.code, detail[:500])
        raise LLMError(
            f'Erreur API Gemini ({exc.code}).',
            status_code=502,
            payload={'detail': detail[:1000]},
        )
    except urllib.error.URLError as exc:
        raise LLMError(f'Gemini injoignable: {exc.reason}', status_code=502)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMError(f'Réponse Gemini illisible: {exc}', status_code=502)

    return _extract_text(payload)


def _extract_text(payload: dict) -> str:
    candidates = payload.get('candidates') or []
    if not candidates:
        raise LLMError('Réponse Gemini sans candidat.', status_code=502, payload=payload)
    parts = (candidates[0].get('content') or {}).get('parts') or []
    text = ''.join(part.get('text', '') for part in parts).strip()
    if not text:
        raise LLMError('Réponse Gemini vide.', status_code=502, payload=payload)
    return text


def generate_json(prompt: str, **kwargs) -> dict:
    """Comme generate() mais renvoie un dict JSON (parsing tolérant)."""
    kwargs.setdefault('json_mode', True)
    text = generate(prompt, **kwargs)
    return _coerce_json(text)


def _coerce_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise LLMError('Le LLM n\'a pas renvoyé de JSON valide.', status_code=502, payload={'raw': text[:1000]})
