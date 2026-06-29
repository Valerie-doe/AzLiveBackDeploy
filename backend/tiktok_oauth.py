import hashlib
import json
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


def exchange_code_for_tokens(code: str, code_verifier: str) -> dict[str, Any]:
    payload = _tiktok_request(
        TIKTOK_TOKEN_URL,
        {
            'client_key': settings.TIKTOK_CLIENT_KEY,
            'client_secret': settings.TIKTOK_CLIENT_SECRET,
            'code': code,
            'grant_type': 'authorization_code',
            'redirect_uri': settings.TIKTOK_REDIRECT_URI,
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

    display_name = profile.get('display_name') or 'Vendeur TikTok'
    username = profile.get('username') or ''
    tiktok_username = f'@{username.lstrip("@")}' if username else display_name

    access_token = token_payload.get('access_token', '')
    refresh_token = token_payload.get('refresh_token')

    existing = Vendeur.objects.filter(tiktok_open_id=open_id).select_related('user').first()
    if existing:
        existing.tiktok_access_token = access_token
        existing.tiktok_refresh_token = refresh_token
        existing.tiktok_username = tiktok_username
        if display_name and existing.nom != display_name:
            existing.nom = display_name
        existing.save(
            update_fields=['tiktok_access_token', 'tiktok_refresh_token', 'tiktok_username', 'nom']
        )
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
        tiktok_username=tiktok_username,
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
