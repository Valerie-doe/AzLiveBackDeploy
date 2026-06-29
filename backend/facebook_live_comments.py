"""Poller des commentaires d'un Live Facebook.

Les commentaires d'une vidéo Live ne sont pas livrés de façon fiable par le webhook
`feed` (réservé aux posts). Le canal officiel est l'API des commentaires de live :
`GET /{live_video_id}/comments`. Pendant qu'un live est en cours, un thread interroge
cet endpoint à intervalle régulier et réinjecte chaque nouveau commentaire dans
`process_social_comment` (toute la logique JP/dressing/insertion existante est réutilisée).
"""
import logging
import threading
from typing import Any

from django.db import close_old_connections

from .facebook_oauth import _graph_request, facebook_configured
from .jp_capture import JPCaptureError, process_social_comment
from .models import Live, PageFacebook

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5
COMMENT_BATCH_LIMIT = 50
COMMENT_FIELDS = 'id,message,created_time,from{id,name}'

_listeners: dict[int, '_FacebookCommentListener'] = {}
_listeners_lock = threading.Lock()


def _fetch_live_comments(live_video_id: str, access_token: str) -> list[dict[str, Any]]:
    """Récupère les commentaires les plus récents du live (ordre antéchronologique)."""
    payload = _graph_request(
        f'{live_video_id}/comments',
        {
            'access_token': access_token,
            'fields': COMMENT_FIELDS,
            'live_filter': 'no_filter',
            'order': 'reverse_chronological',
            'limit': COMMENT_BATCH_LIMIT,
        },
        method='GET',
    )
    if isinstance(payload, dict):
        data = payload.get('data')
        return data if isinstance(data, list) else []
    return []


class _FacebookCommentListener(threading.Thread):
    daemon = True

    def __init__(
        self,
        live_id: int,
        live_video_id: str,
        page_id: str | None,
        access_token: str,
        stop_event: threading.Event,
    ):
        super().__init__(name=f'fb-comments-{live_id}')
        self.live_id = live_id
        self.live_video_id = str(live_video_id)
        self.page_id = str(page_id) if page_id else None
        self.access_token = access_token
        self.stop_event = stop_event
        self._seen_ids: set[str] = set()

    def run(self):
        while not self.stop_event.is_set():
            self._poll_once()
            if self.stop_event.wait(POLL_INTERVAL_SECONDS):
                break

    def _poll_once(self):
        close_old_connections()

        live = Live.objects.filter(pk=self.live_id).select_related('vendeur').first()
        if not live or live.statut != Live.STATUT_EN_COURS:
            # Le live est terminé ou supprimé : on arrête le poller.
            self.stop_event.set()
            return

        try:
            comments = _fetch_live_comments(self.live_video_id, self.access_token)
        except Exception as exc:  # noqa: BLE001 — réseau/API : on log et on retentera.
            logger.warning('Récupération commentaires FB échouée (live #%s): %s', self.live_id, exc)
            return

        # L'API renvoie du plus récent au plus ancien : on traite les nouveaux du plus
        # ancien au plus récent pour préserver l'ordre des JP (ordre_jp).
        new_comments = [c for c in comments if str(c.get('id') or '') not in self._seen_ids]
        new_comments.reverse()

        for comment in new_comments:
            comment_id = str(comment.get('id') or '')
            if not comment_id:
                continue
            self._seen_ids.add(comment_id)

            sender = comment.get('from') or {}
            sender_id = str(sender.get('id') or '')
            sender_name = sender.get('name') or 'Client Facebook'
            message = comment.get('message') or ''
            if not sender_id or not message:
                # Auteur masqué (confidentialité / hors rôle app) ou commentaire vide.
                continue

            try:
                result = process_social_comment(
                    sender_id=sender_id,
                    sender_name=sender_name,
                    comment_text=message,
                    channel='Facebook',
                    page_id=self.page_id,
                    vendeur=live.vendeur,
                    live=live,
                    id_field='facebook_id',
                    comment_id=comment_id,
                )
                if result.get('status') != 'ignored':
                    logger.info(
                        'JP Facebook capturé (live #%s, commentaire %s): %s',
                        self.live_id,
                        comment_id,
                        result.get('status'),
                    )
            except JPCaptureError as exc:
                logger.info('Commentaire FB non capturé (live #%s): %s', self.live_id, exc.message)
            except Exception as exc:  # noqa: BLE001
                logger.warning('Erreur capture JP Facebook (live #%s): %s', self.live_id, exc)


def _select_target_broadcast(broadcasts: list[dict[str, Any]]) -> dict[str, Any] | None:
    for broadcast in broadcasts:
        if broadcast.get('demo'):
            continue
        if broadcast.get('live_video_id') and broadcast.get('page_id'):
            return broadcast
    return None


def start_facebook_comment_listener(
    live: Live,
    broadcasts: list[dict[str, Any]],
    pages: list[PageFacebook],
) -> bool:
    """Démarre le poller de commentaires pour le premier broadcast Facebook réel du live."""
    if not facebook_configured() or live.vendeur.is_demo_mode:
        return False

    target = _select_target_broadcast(broadcasts)
    if not target:
        return False

    pages_by_id = {str(page.page_id): page for page in pages}
    page = pages_by_id.get(str(target['page_id']))
    access_token = page.access_token if page else None
    if not access_token:
        return False

    stop_event = threading.Event()
    with _listeners_lock:
        stop_facebook_comment_listener(live, lock_held=True)
        listener = _FacebookCommentListener(
            live.pk,
            target['live_video_id'],
            target['page_id'],
            access_token,
            stop_event,
        )
        _listeners[live.pk] = listener
        listener.start()

    logger.info(
        'Poller commentaires Facebook démarré (live #%s, video %s)',
        live.pk,
        target['live_video_id'],
    )
    return True


def stop_facebook_comment_listener(live: Live, lock_held: bool = False) -> bool:
    live_id = live.pk

    def _stop():
        listener = _listeners.pop(live_id, None)
        if not listener:
            return False
        listener.stop_event.set()
        return True

    if lock_held:
        return _stop()

    with _listeners_lock:
        return _stop()


def listener_status(live_id: int) -> dict[str, Any]:
    with _listeners_lock:
        listener = _listeners.get(live_id)
        if not listener:
            return {'running': False}
        return {
            'running': listener.is_alive(),
            'live_video_id': listener.live_video_id,
            'thread': listener.name,
        }
