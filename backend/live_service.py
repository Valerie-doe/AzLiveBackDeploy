from django.db import transaction
from django.utils import timezone

from .facebook_live import (
    FacebookLiveError,
    create_demo_tiktok_broadcast,
    resolve_live_pages,
    start_facebook_broadcasts,
    stop_facebook_broadcasts,
)
from .facebook_live_comments import (
    start_facebook_comment_listener,
    stop_facebook_comment_listener,
)
from .facebook_oauth import FacebookOAuthError, facebook_configured
from .facebook_webhooks import subscribe_vendeur_pages
from .mediamtx import MediaMTXError, mediamtx_enabled, provision_live_path, teardown_live_path
from .models import Live, PageFacebook
from .tiktool_live import (
    build_tiktok_diffusion,
    start_tiktool_listener,
    stop_tiktool_listener,
    tiktool_configured,
)


class LiveServiceError(Exception):
    def __init__(self, message, status_code=400, payload=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload or {}


def _pages_by_id(pages: list[PageFacebook]) -> dict[str, PageFacebook]:
    return {str(page.page_id): page for page in pages}


def _stop_other_active_lives(vendeur, exclude_live_id: int | None = None):
    queryset = Live.objects.filter(vendeur=vendeur, statut=Live.STATUT_EN_COURS)
    if exclude_live_id:
        queryset = queryset.exclude(pk=exclude_live_id)

    for other_live in queryset:
        arreter_live(other_live, auto=True)


def _ensure_webhooks(vendeur):
    if not facebook_configured() or vendeur.is_demo_mode:
        return
    try:
        subscribe_vendeur_pages(vendeur)
    except FacebookOAuthError:
        pass


def _first_secure_stream_url(facebook_broadcasts: list[dict]) -> str | None:
    for broadcast in facebook_broadcasts:
        if broadcast.get('demo'):
            continue
        url = broadcast.get('secure_stream_url') or broadcast.get('stream_url')
        if url:
            return url
    return None


def _provision_webrtc(live: Live, facebook_broadcasts: list[dict]) -> dict | None:
    """Prépare le pont navigateur -> Facebook via MediaMTX (si activé)."""
    if not mediamtx_enabled() or live.vendeur.is_demo_mode:
        return None
    secure_stream_url = _first_secure_stream_url(facebook_broadcasts)
    if not secure_stream_url:
        return None
    try:
        ingest = provision_live_path(live, secure_stream_url)
    except MediaMTXError as exc:
        # Le live Facebook existe déjà ; on n'échoue pas, mais on signale l'absence de pont.
        return {'status': 'error', 'detail': exc.message}
    return {
        'status': 'ready',
        'path': ingest['path'],
        'whip_url': ingest['whip_url'],
        'publish_token': ingest['publish_token'],
    }


@transaction.atomic
def demarrer_live(live: Live) -> Live:
    if live.statut == Live.STATUT_EN_COURS and live.diffusion_plateformes:
        return live

    _stop_other_active_lives(live.vendeur, exclude_live_id=live.pk)

    pages = resolve_live_pages(live)
    try:
        facebook_broadcasts = start_facebook_broadcasts(live, pages)
    except FacebookLiveError as exc:
        raise LiveServiceError(exc.message, status_code=exc.status_code) from exc

    tiktok_broadcast = None
    if live.vendeur.tiktok_username:
        if live.vendeur.is_demo_mode:
            tiktok_broadcast = create_demo_tiktok_broadcast(live.vendeur)
        else:
            tiktok_broadcast = build_tiktok_diffusion(live)

    if not facebook_broadcasts and not tiktok_broadcast and not live.vendeur.is_demo_mode:
        if not pages:
            raise LiveServiceError(
                'Aucune page Facebook sélectionnée. Synchronisez vos pages ou choisissez-en dans le live.',
                status_code=400,
            )

    _ensure_webhooks(live.vendeur)

    webrtc = _provision_webrtc(live, facebook_broadcasts)

    diffusion = {
        'facebook': facebook_broadcasts,
        'tiktok': tiktok_broadcast,
        'webrtc': webrtc,
        'started_at': timezone.now().isoformat(),
    }

    live.statut = Live.STATUT_EN_COURS
    live.date_debut = timezone.now()
    live.date_live = live.date_debut
    live.date_fin = None
    live.diffusion_plateformes = diffusion
    live.save(
        update_fields=[
            'statut',
            'date_debut',
            'date_live',
            'date_fin',
            'diffusion_plateformes',
        ]
    )

    if tiktok_broadcast and tiktool_configured() and not live.vendeur.is_demo_mode:
        started = start_tiktool_listener(live)
        if started:
            diffusion = dict(live.diffusion_plateformes or {})
            tiktok_state = dict(diffusion.get('tiktok') or {})
            tiktok_state['listener'] = 'running'
            diffusion['tiktok'] = tiktok_state
            live.diffusion_plateformes = diffusion
            live.save(update_fields=['diffusion_plateformes'])

    # Capture automatique des commentaires JP du Live Facebook (polling API live comments).
    if facebook_broadcasts and facebook_configured() and not live.vendeur.is_demo_mode:
        start_facebook_comment_listener(live, facebook_broadcasts, pages)

    return live


@transaction.atomic
def arreter_live(live: Live, auto: bool = False) -> Live:
    if live.statut == Live.STATUT_TERMINE and not live.diffusion_plateformes:
        return live

    diffusion = dict(live.diffusion_plateformes or {})
    facebook_broadcasts = list(diffusion.get('facebook') or [])
    pages = resolve_live_pages(live)
    stop_facebook_broadcasts(facebook_broadcasts, _pages_by_id(pages))

    tiktok = diffusion.get('tiktok')
    if isinstance(tiktok, dict):
        tiktok = {**tiktok, 'status': 'ENDED', 'listener': 'stopped'}
        diffusion['tiktok'] = tiktok

    webrtc = diffusion.get('webrtc')
    if isinstance(webrtc, dict) and webrtc.get('path'):
        teardown_live_path(webrtc['path'])
        diffusion['webrtc'] = {**webrtc, 'status': 'stopped', 'publish_token': None}

    stop_tiktool_listener(live)
    stop_facebook_comment_listener(live)

    diffusion['facebook'] = facebook_broadcasts
    diffusion['stopped_at'] = timezone.now().isoformat()
    if auto:
        diffusion['stopped_reason'] = 'auto_switch'

    live.statut = Live.STATUT_TERMINE
    live.date_fin = timezone.now()
    live.diffusion_plateformes = diffusion
    live.save(update_fields=['statut', 'date_fin', 'diffusion_plateformes'])
    return live
