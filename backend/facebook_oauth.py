import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from django.conf import settings
from django.contrib.auth.models import User
from django.core.signing import BadSignature, Signer
from rest_framework.authtoken.models import Token

from .models import PageFacebook, Vendeur

GRAPH_API_VERSION = 'v21.0'
STATE_SIGNER = Signer(salt='azlive-facebook-oauth-state')


class FacebookOAuthError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def facebook_configured() -> bool:
    return bool(settings.FACEBOOK_APP_ID and settings.FACEBOOK_APP_SECRET)


def _graph_request(path, params=None, access_token=None, method='GET', timeout=None):
    query = dict(params or {})
    if access_token and 'access_token' not in query:
        query['access_token'] = access_token

    base_url = f'https://graph.facebook.com/{GRAPH_API_VERSION}/{path.lstrip("/")}'
    data = None
    headers = {'User-Agent': 'AZLive/1.0'}

    if method.upper() in ('POST', 'DELETE'):
        url = base_url
        data = urllib.parse.urlencode(query).encode('utf-8')
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
        request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    else:
        url = base_url
        if query:
            url = f'{url}?{urllib.parse.urlencode(query)}'
        request = urllib.request.Request(url, headers=headers, method='GET')

    request_timeout = timeout or getattr(settings, 'FACEBOOK_GRAPH_TIMEOUT', 45)
    max_retries = max(1, int(getattr(settings, 'FACEBOOK_GRAPH_RETRIES', 3)))
    last_error: Exception | None = None

    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(request, timeout=request_timeout) as response:
                return json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode('utf-8'))
                message = payload.get('error', {}).get('message', str(exc))
            except (json.JSONDecodeError, UnicodeDecodeError):
                message = str(exc)
            raise FacebookOAuthError(message, status_code=exc.code) from exc
        except urllib.error.URLError as exc:
            last_error = exc
            if attempt < max_retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise FacebookOAuthError(
                f'Impossible de contacter Facebook: {exc.reason}',
                status_code=503,
            ) from exc
        except TimeoutError as exc:
            last_error = exc
            if attempt < max_retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise FacebookOAuthError(
                f'Impossible de contacter Facebook: délai dépassé ({request_timeout}s)',
                status_code=503,
            ) from exc

    raise FacebookOAuthError(
        f'Impossible de contacter Facebook: {last_error}',
        status_code=503,
    )


def generate_oauth_state() -> str:
    return STATE_SIGNER.sign(secrets.token_urlsafe(24))


def validate_oauth_state(state: str) -> None:
    if not state:
        raise FacebookOAuthError('Le paramètre state est requis.')
    try:
        STATE_SIGNER.unsign(state)
    except BadSignature as exc:
        raise FacebookOAuthError('State OAuth invalide ou expiré.') from exc


def build_oauth_url(state: str) -> str:
    params = {
        'client_id': settings.FACEBOOK_APP_ID,
        'redirect_uri': settings.FACEBOOK_REDIRECT_URI,
        'state': state,
        'response_type': 'code',
    }
    config_id = getattr(settings, 'FACEBOOK_CONFIG_ID', '')
    if config_id:
        # Facebook Login for Business : les permissions viennent de la configuration
        params['config_id'] = config_id
    else:
        # Facebook Login classique : permissions via scope
        params['scope'] = settings.FACEBOOK_OAUTH_SCOPES
    return f'https://www.facebook.com/{GRAPH_API_VERSION}/dialog/oauth?{urllib.parse.urlencode(params)}'


def exchange_code_for_token(code: str) -> str:
    payload = _graph_request(
        'oauth/access_token',
        {
            'client_id': settings.FACEBOOK_APP_ID,
            'client_secret': settings.FACEBOOK_APP_SECRET,
            'redirect_uri': settings.FACEBOOK_REDIRECT_URI,
            'code': code,
        },
    )
    access_token = payload.get('access_token')
    if not access_token:
        raise FacebookOAuthError('Facebook n\'a pas renvoyé de token d\'accès.')
    return access_token


def exchange_for_long_lived_token(short_lived_token: str) -> str:
    payload = _graph_request(
        'oauth/access_token',
        {
            'grant_type': 'fb_exchange_token',
            'client_id': settings.FACEBOOK_APP_ID,
            'client_secret': settings.FACEBOOK_APP_SECRET,
            'fb_exchange_token': short_lived_token,
        },
    )
    return payload.get('access_token') or short_lived_token


def get_user_profile(access_token: str) -> dict[str, Any]:
    return _graph_request('me', {'fields': 'id,name,email'}, access_token=access_token)


def get_user_pages(access_token: str) -> list[dict[str, Any]]:
    payload = _graph_request(
        'me/accounts',
        {'fields': 'id,name,access_token,category'},
        access_token=access_token,
    )
    return payload.get('data', [])


def sync_vendeur_pages(vendeur: Vendeur, access_token: str | None = None) -> list[PageFacebook]:
    token = access_token or vendeur.facebook_access_token
    if not token:
        raise FacebookOAuthError(
            'Aucun token Facebook disponible. Connectez-vous d\'abord via Facebook.',
            status_code=401,
        )

    pages_data = get_user_pages(token)
    synced_pages = []
    seen_page_ids = set()

    for page in pages_data:
        page_id = page.get('id')
        if not page_id:
            continue
        seen_page_ids.add(page_id)
        page_obj, _ = PageFacebook.objects.update_or_create(
            vendeur=vendeur,
            page_id=page_id,
            defaults={
                'nom': page.get('name', page_id),
                'statut': PageFacebook.STATUT_PRET,
                'access_token': page.get('access_token'),
            },
        )
        synced_pages.append(page_obj)

    vendeur.pages_facebook.exclude(page_id__in=seen_page_ids).delete()

    if synced_pages:
        primary = synced_pages[0]
        vendeur.facebook_page_id = primary.page_id
        vendeur.facebook_page_name = primary.nom
        vendeur.is_demo_mode = False
    else:
        vendeur.facebook_page_id = None
        vendeur.facebook_page_name = None

    vendeur.save(update_fields=['facebook_page_id', 'facebook_page_name', 'is_demo_mode'])
    return synced_pages


def get_or_create_vendeur_from_facebook(profile: dict[str, Any], access_token: str) -> tuple[Vendeur, User, bool]:
    fb_user_id = profile.get('id')
    if not fb_user_id:
        raise FacebookOAuthError('Profil Facebook invalide.')

    name = profile.get('name') or 'Vendeur Facebook'
    email = profile.get('email') or ''

    existing = Vendeur.objects.filter(facebook_user_id=fb_user_id).select_related('user').first()
    if existing:
        existing.facebook_access_token = access_token
        if name and existing.nom != name:
            existing.nom = name
        existing.save(update_fields=['facebook_access_token', 'nom'])
        user = existing.user
        if not user:
            user = _create_user_for_vendeur(existing, fb_user_id, email, name)
        return existing, user, False

    user = None
    if email:
        user = User.objects.filter(email=email).first()

    if user:
        vendeur, created = Vendeur.objects.get_or_create(
            user=user,
            defaults={
                'nom': name,
                'contact': '',
                'facebook_user_id': fb_user_id,
                'facebook_access_token': access_token,
            },
        )
        if not created:
            vendeur.facebook_user_id = fb_user_id
            vendeur.facebook_access_token = access_token
            vendeur.nom = name
            vendeur.save(update_fields=['facebook_user_id', 'facebook_access_token', 'nom'])
        return vendeur, user, created

    user = User.objects.create_user(
        username=f'fb_{fb_user_id}',
        email=email,
        first_name=name.split(' ', 1)[0] if name else '',
        last_name=name.split(' ', 1)[1] if name and ' ' in name else '',
    )
    user.set_unusable_password()
    user.save()

    vendeur = Vendeur.objects.create(
        user=user,
        nom=name,
        contact='',
        facebook_user_id=fb_user_id,
        facebook_access_token=access_token,
    )
    return vendeur, user, True


def _create_user_for_vendeur(vendeur: Vendeur, fb_user_id: str, email: str, name: str) -> User:
    user = User.objects.create_user(
        username=f'fb_{fb_user_id}',
        email=email,
        first_name=name.split(' ', 1)[0] if name else '',
        last_name=name.split(' ', 1)[1] if name and ' ' in name else '',
    )
    user.set_unusable_password()
    user.save()
    vendeur.user = user
    vendeur.save(update_fields=['user'])
    return user


def issue_auth_token(user: User) -> str:
    token, _ = Token.objects.get_or_create(user=user)
    return token.key


def authenticate_with_access_token(access_token: str) -> tuple[Vendeur, User, bool, str]:
    profile = get_user_profile(access_token)
    long_lived_token = exchange_for_long_lived_token(access_token)
    vendeur, user, created = get_or_create_vendeur_from_facebook(profile, long_lived_token)
    auth_token = issue_auth_token(user)
    return vendeur, user, created, auth_token


def authenticate_with_code(code: str, state: str) -> tuple[Vendeur, User, bool, str]:
    validate_oauth_state(state)
    short_lived_token = exchange_code_for_token(code)
    return authenticate_with_access_token(short_lived_token)
