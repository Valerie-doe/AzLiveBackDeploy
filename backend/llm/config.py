"""Configuration de l'intégration LLM (lue depuis les variables d'environnement).

Les variables sont chargées depuis `.env` au démarrage de Django
(via `_load_dotenv` dans settings.py), donc disponibles dans `os.environ`.
On lit ici directement l'environnement pour ne pas avoir à modifier settings.py.
"""
import os


def _env_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


# Clé API Google AI Studio (https://aistudio.google.com/app/apikey)
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')

# Modèle Gemini (gratuit/rapide par défaut)
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-2.0-flash')

# Base de l'API REST Gemini
GEMINI_API_BASE = os.environ.get(
    'GEMINI_API_BASE',
    'https://generativelanguage.googleapis.com/v1beta',
)

# Délai max d'un appel LLM (secondes)
try:
    LLM_TIMEOUT = float(os.environ.get('LLM_TIMEOUT', '20'))
except (TypeError, ValueError):
    LLM_TIMEOUT = 20.0

# LLM activé seulement si une clé est fournie, sauf override explicite LLM_ENABLED.
LLM_ENABLED = _env_bool(os.environ.get('LLM_ENABLED'), default=bool(GEMINI_API_KEY))


def llm_status() -> dict:
    """Etat courant de la configuration LLM (sans exposer la clé)."""
    return {
        'enabled': LLM_ENABLED,
        'provider': 'google-gemini',
        'model': GEMINI_MODEL,
        'api_key_configured': bool(GEMINI_API_KEY),
        'timeout_seconds': LLM_TIMEOUT,
    }
