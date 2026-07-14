import hashlib
import json
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from django.conf import settings
from django.contrib.auth.models import User
from django.core.signing import BadSignature, Signer

from .facebook_oauth import issue_auth_token
from .models import Vendeur

STATE_SIGNER = Signer(salt='azlive-tiktok-oauth-state')
PUBLIC_STATE_SIGNER = Signer(salt='azlive-tiktok-public-order')
TIKTOK_AUTH_URL = 'https://www.tiktok.com/v2/auth/authorize/'
TIKTOK_TOKEN_URL = 'https://open.tiktokapis.com/v2/oauth/token/'
TIKTOK_USER_INFO_URL = 'https://open.tiktokapis.com/v2/user/info/'


class TikTokOAuthError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def tiktok_configured() -> bool:
    return bool(settings.TIKTOK_CLIENT_KEY and settings.TIKTOK_CLIENT_SECRET)


def _tiktok_request(url, params=None, method='GET', bearer_token=None):
    headers = {'User-Agent': 'AZLive/1.0'}
    data = None

    if method.upper() == 'POST':
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
        data = urllib.parse.urlencode(params or {}).encode('utf-8')
        request = urllib.request.Request(url, data=data, headers=headers, method='POST')
    else:
        query = urllib.parse.urlencode(params or {})
        full_url = f'{url}?{query}' if query else url
        if bearer_token:
            headers['Authorization'] = f'Bearer {bearer_token}'
        request = urllib.request.Request(full_url, headers=headers, method='GET')

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode('utf-8'))
            message = payload.get('error_description') or payload.get('error', {}).get('message') or str(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            message = str(exc)
        raise TikTokOAuthError(message, status_code=exc.code) from exc
    except urllib.error.URLError as exc:
        raise TikTokOAuthError(f'Impossible de contacter TikTok: {exc.reason}', status_code=503) from exc


def _generate_code_verifier() -> str:
    return secrets.token_urlsafe(48)[:64]


def _generate_code_challenge(code_verifier: str) -> str:
    return hashlib.sha256(code_verifier.encode('utf-8')).hexdigest()


def generate_oauth_state() -> tuple[str, str]:
    verifier = _generate_code_verifier()
    challenge = _generate_code_challenge(verifier)
    nonce = secrets.token_urlsafe(24)
    state = STATE_SIGNER.sign(f'{nonce}|{verifier}')
    return state, challenge


def validate_oauth_state(state: str) -> str:
    if not state:
        raise TikTokOAuthError('Le paramètre state est requis.')
    try:
        unsigned = STATE_SIGNER.unsign(state)
    except BadSignature as exc:
        raise TikTokOAuthError('State OAuth invalide ou expiré.') from exc
    if '|' not in unsigned:
        raise TikTokOAuthError('State OAuth invalide ou expiré.')
    _, verifier = unsigned.split('|', 1)
    if not verifier:
        raise TikTokOAuthError('State OAuth invalide ou expiré.')
    return verifier


def build_oauth_url(state: str, code_challenge: str) -> str:
    params = {
        'client_key': settings.TIKTOK_CLIENT_KEY,
        'response_type': 'code',
        'scope': settings.TIKTOK_OAUTH_SCOPES,
        'redirect_uri': settings.TIKTOK_REDIRECT_URI,
        'state': state,
        'code_challenge': code_challenge,
        'code_challenge_method': 'S256',
    }
    return f'{TIKTOK_AUTH_URL}?{urllib.parse.urlencode(params)}'


def exchange_code_for_tokens(
    code: str,
    code_verifier: str,
    redirect_uri: str | None = None,
) -> dict[str, Any]:
    payload = _tiktok_request(
        TIKTOK_TOKEN_URL,
        {
            'client_key': settings.TIKTOK_CLIENT_KEY,
            'client_secret': settings.TIKTOK_CLIENT_SECRET,
            'code': code,
            'grant_type': 'authorization_code',
            'redirect_uri': redirect_uri or settings.TIKTOK_REDIRECT_URI,
            'code_verifier': code_verifier,
        },
        method='POST',
    )
    if payload.get('error'):
        raise TikTokOAuthError(payload.get('error_description') or payload.get('error'))
    if not payload.get('access_token'):
        raise TikTokOAuthError('TikTok n\'a pas renvoyé de token d\'accès.')
    return payload


def get_user_profile(access_token: str) -> dict[str, Any]:
    # username requiert user.info.profile — on ne demande que user.info.basic ici
    payload = _tiktok_request(
        TIKTOK_USER_INFO_URL,
        {'fields': 'open_id,union_id,avatar_url,display_name'},
        bearer_token=access_token,
    )
    error = payload.get('error') or {}
    if error.get('code') not in (None, '', 'ok'):
        raise TikTokOAuthError(error.get('message') or 'Profil TikTok inaccessible.')
    user = (payload.get('data') or {}).get('user') or {}
    if not user.get('open_id'):
        raise TikTokOAuthError('Profil TikTok invalide.')
    return user


def get_or_create_vendeur_from_tiktok(profile: dict[str, Any], token_payload: dict[str, Any]) -> tuple[Vendeur, User, bool]:
    open_id = profile.get('open_id') or token_payload.get('open_id')
    if not open_id:
        raise TikTokOAuthError('Identifiant TikTok manquant.')

    from .jp_capture import normalize_tiktok_username

    display_name = profile.get('display_name') or 'Vendeur TikTok'
    username = (profile.get('username') or '').strip()
    # Ne jamais stocker le display_name (souvent avec emoji/espaces) comme @TikTok.
    # Sans username OAuth, on conserve l'existant s'il est valide.
    tiktok_username = f'@{username.lstrip("@")}' if username else ''

    def _is_valid_handle(value: str) -> bool:
        return bool(re.fullmatch(r'[a-z0-9._-]+', normalize_tiktok_username(value)))

    access_token = token_payload.get('access_token', '')
    refresh_token = token_payload.get('refresh_token')

    existing = Vendeur.objects.filter(tiktok_open_id=open_id).select_related('user').first()
    if existing:
        existing.tiktok_access_token = access_token
        existing.tiktok_refresh_token = refresh_token
        update_fields = ['tiktok_access_token', 'tiktok_refresh_token']
        if tiktok_username and _is_valid_handle(tiktok_username):
            existing.tiktok_username = tiktok_username
            update_fields.append('tiktok_username')
        if display_name and existing.nom != display_name:
            existing.nom = display_name
            update_fields.append('nom')
        existing.save(update_fields=update_fields)
        user = existing.user
        if not user:
            user = _create_user_for_vendeur(existing, open_id, display_name)
        return existing, user, False

    user = User.objects.create_user(
        username=f'tt_{open_id[:32]}',
        first_name=display_name.split(' ', 1)[0] if display_name else '',
        last_name=display_name.split(' ', 1)[1] if display_name and ' ' in display_name else '',
    )
    user.set_unusable_password()
    user.save()

    vendeur = Vendeur.objects.create(
        user=user,
        nom=display_name,
        contact='',
        tiktok_open_id=open_id,
        tiktok_username=tiktok_username or None,
        tiktok_access_token=access_token,
        tiktok_refresh_token=refresh_token,
    )
    return vendeur, user, True


def _create_user_for_vendeur(vendeur: Vendeur, open_id: str, name: str) -> User:
    user = User.objects.create_user(
        username=f'tt_{open_id[:32]}',
        first_name=name.split(' ', 1)[0] if name else '',
        last_name=name.split(' ', 1)[1] if name and ' ' in name else '',
    )
    user.set_unusable_password()
    user.save()
    vendeur.user = user
    vendeur.save(update_fields=['user'])
    return user


def authenticate_with_code(code: str, state: str) -> tuple[Vendeur, User, bool, str]:
    code_verifier = validate_oauth_state(state)
    token_payload = exchange_code_for_tokens(code, code_verifier)
    profile = get_user_profile(token_payload['access_token'])
    vendeur, user, created = get_or_create_vendeur_from_tiktok(profile, token_payload)
    auth_token = issue_auth_token(user)
    return vendeur, user, created, auth_token


def authenticate_with_access_token(access_token: str) -> tuple[Vendeur, User, bool, str]:
    profile = get_user_profile(access_token)
    token_payload = {'access_token': access_token, 'open_id': profile.get('open_id')}
    vendeur, user, created = get_or_create_vendeur_from_tiktok(profile, token_payload)
    auth_token = issue_auth_token(user)
    return vendeur, user, created, auth_token


# --- OAuth client public (formulaire de commande live TikTok) ---


def generate_public_oauth_state(live_id: int) -> tuple[str, str]:
    verifier = _generate_code_verifier()
    challenge = _generate_code_challenge(verifier)
    nonce = secrets.token_urlsafe(16)
    state = PUBLIC_STATE_SIGNER.sign(f'{live_id}|{nonce}|{verifier}')
    return state, challenge


def validate_public_oauth_state(state: str) -> tuple[int, str]:
    if not state:
        raise TikTokOAuthError('Le paramètre state est requis.')
    try:
        unsigned = PUBLIC_STATE_SIGNER.unsign(state)
    except BadSignature as exc:
        raise TikTokOAuthError('State OAuth invalide ou expiré.') from exc
    parts = unsigned.split('|', 2)
    if len(parts) != 3:
        raise TikTokOAuthError('State OAuth invalide ou expiré.')
    live_id_str, _, verifier = parts
    if not verifier:
        raise TikTokOAuthError('State OAuth invalide ou expiré.')
    try:
        live_id = int(live_id_str)
    except ValueError as exc:
        raise TikTokOAuthError('State OAuth invalide ou expiré.') from exc
    return live_id, verifier


def build_public_oauth_url(state: str, code_challenge: str) -> str:
    # Même redirect_uri que la connexion vendeur (déjà enregistrée dans le portail TikTok).
    redirect_uri = settings.TIKTOK_REDIRECT_URI
    scopes = settings.TIKTOK_PUBLIC_OAUTH_SCOPES
    params = {
        'client_key': settings.TIKTOK_CLIENT_KEY,
        'response_type': 'code',
        'scope': scopes,
        'redirect_uri': redirect_uri,
        'state': state,
        'code_challenge': code_challenge,
        'code_challenge_method': 'S256',
    }
    return f'{TIKTOK_AUTH_URL}?{urllib.parse.urlencode(params)}'


def get_public_user_profile(access_token: str) -> dict[str, Any]:
    """Profil TikTok pour un client (formulaire public) — inclut username si scope accordé."""
    payload = _tiktok_request(
        TIKTOK_USER_INFO_URL,
        {'fields': 'open_id,union_id,avatar_url,display_name,username'},
        bearer_token=access_token,
    )
    error = payload.get('error') or {}
    if error.get('code') not in (None, '', 'ok'):
        raise TikTokOAuthError(error.get('message') or 'Profil TikTok inaccessible.')
    user = (payload.get('data') or {}).get('user') or {}
    if not user.get('open_id'):
        raise TikTokOAuthError('Profil TikTok invalide.')
    return user


def resolve_public_client_handle(profile: dict[str, Any]) -> str:
    """Identifiant utilisable pour retrouver les commandes capturées (uniqueId / @)."""
    from .jp_capture import normalize_tiktok_username

    username = profile.get('username') or ''
    if username:
        return normalize_tiktok_username(username)
    open_id = profile.get('open_id') or ''
    if open_id:
        return str(open_id)
    raise TikTokOAuthError('Impossible d\'identifier votre compte TikTok.')


def authenticate_public_client_with_code(code: str, state: str) -> tuple[int, str]:
    """Échange le code OAuth et renvoie (live_id, handle) sans créer de compte vendeur."""
    live_id, code_verifier = validate_public_oauth_state(state)
    token_payload = exchange_code_for_tokens(
        code, code_verifier, redirect_uri=settings.TIKTOK_REDIRECT_URI
    )
    profile = get_public_user_profile(token_payload['access_token'])
    handle = resolve_public_client_handle(profile)
    return live_id, handle
