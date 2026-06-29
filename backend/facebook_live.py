import uuid
from typing import Any

from django.conf import settings
from django.utils import timezone

from .facebook_oauth import FacebookOAuthError, _graph_request, facebook_configured
from .models import Live, PageFacebook


class FacebookLiveError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def resolve_live_pages(live: Live):
    vendeur = live.vendeur
    selected = live.pages_facebook or []
    queryset = PageFacebook.objects.filter(vendeur=vendeur)

    if selected:
        pages = []
        for item in selected:
            page = queryset.filter(nom=item).first() or queryset.filter(page_id=str(item)).first()
            if page:
                pages.append(page)
        return pages

    return list(queryset.filter(statut=PageFacebook.STATUT_PRET))


def create_facebook_live_broadcast(page: PageFacebook, title: str, description: str = '') -> dict[str, Any]:
    if not page.access_token:
        raise FacebookLiveError(f"Aucun token pour la page {page.nom}.")

    payload = _graph_request(
        f'{page.page_id}/live_videos',
        {
            'title': title,
            'description': description or title,
            'status': settings.FACEBOOK_LIVE_STATUS,
            'access_token': page.access_token,
        },
        method='POST',
    )
    return {
        'page_id': page.page_id,
        'page_name': page.nom,
        'live_video_id': payload.get('id'),
        'status': 'LIVE',
        'stream_url': payload.get('stream_url'),
        'secure_stream_url': payload.get('secure_stream_url'),
        'embed_url': payload.get('embed_html'),
    }


def end_facebook_live_broadcast(live_video_id: str, page_access_token: str) -> dict[str, Any]:
    return _graph_request(
        live_video_id,
        {
            'end_live_video': 'true',
            'access_token': page_access_token,
        },
        method='POST',
    )


def create_demo_facebook_broadcasts(live: Live, pages: list[PageFacebook]) -> list[dict[str, Any]]:
    broadcasts = []
    for page in pages:
        broadcasts.append(
            {
                'page_id': page.page_id,
                'page_name': page.nom,
                'live_video_id': f'demo_live_{page.page_id}_{uuid.uuid4().hex[:8]}',
                'status': 'LIVE',
                'stream_url': f'rtmp://live.demo.azlive/{page.page_id}',
                'embed_url': f'https://facebook.com/{page.page_id}/live/demo',
                'demo': True,
            }
        )
    if not broadcasts and not pages:
        broadcasts.append(
            {
                'page_id': 'fb_page_demo',
                'page_name': live.vendeur.facebook_page_name or 'Page Demo AZLive',
                'live_video_id': f'demo_live_{uuid.uuid4().hex[:8]}',
                'status': 'LIVE',
                'demo': True,
            }
        )
    return broadcasts


def create_demo_tiktok_broadcast(vendeur) -> dict[str, Any] | None:
    username = vendeur.tiktok_username
    if not username:
        return None
    return {
        'live_id': f'demo_tt_{uuid.uuid4().hex[:8]}',
        'username': username,
        'status': 'LIVE',
        'stream_url': f'rtmp://live.demo.azlive/tiktok/{username.lstrip("@")}',
        'demo': True,
    }


def start_facebook_broadcasts(live: Live, pages: list[PageFacebook]) -> list[dict[str, Any]]:
    if not pages:
        return []

    use_demo = live.vendeur.is_demo_mode or not facebook_configured()
    if use_demo:
        return create_demo_facebook_broadcasts(live, pages)

    broadcasts = []
    errors = []
    for page in pages:
        try:
            broadcasts.append(create_facebook_live_broadcast(page, live.titre))
        except FacebookOAuthError as exc:
            errors.append(f'{page.nom}: {exc.message}')

    if errors and not broadcasts:
        raise FacebookLiveError('Impossible de démarrer le live Facebook: ' + '; '.join(errors))

    return broadcasts


def stop_facebook_broadcasts(broadcasts: list[dict[str, Any]], pages_by_id: dict[str, PageFacebook]):
    for broadcast in broadcasts:
        if broadcast.get('demo'):
            broadcast['status'] = 'ENDED'
            continue

        live_video_id = broadcast.get('live_video_id')
        page_id = str(broadcast.get('page_id', ''))
        page = pages_by_id.get(page_id)
        if not live_video_id or not page or not page.access_token:
            broadcast['status'] = 'ENDED'
            continue

        try:
            end_facebook_live_broadcast(live_video_id, page.access_token)
            broadcast['status'] = 'ENDED'
        except FacebookOAuthError:
            broadcast['status'] = 'ENDED'
