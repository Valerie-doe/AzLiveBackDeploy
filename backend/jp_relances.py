from __future__ import annotations

import logging
import threading
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import close_old_connections
from django.db.models import Max
from django.utils import timezone

from backend.message_humanizer import greeting, pick
from backend.models import Commande
from backend.order_confirmation import _order_is_eligible, expire_commande

logger = logging.getLogger(__name__)

_scheduler: '_JpRelanceScheduler | None' = None
_scheduler_lock = threading.Lock()


def process_jp_relances(*, force: bool = False) -> dict[str, Any]:
    now = timezone.now()
    delay = getattr(settings, 'AZLIVE_JP_RELANCE_DELAY_MINUTES', 30)
    max_relances = getattr(settings, 'AZLIVE_JP_MAX_RELANCES', 3)

    inbox_count = 0
    try:
        from backend.facebook_messenger_inbox import sync_pending_messenger_inboxes

        inbox_results = sync_pending_messenger_inboxes()
        inbox_count = len(inbox_results or [])
    except Exception:  # noqa: BLE001
        logger.exception('Inbox Messenger indisponible pendant les relances JP')

    relances: list[dict[str, Any]] = []
    expirations: list[int] = []

    commandes = (
        Commande.objects.filter(statut=Commande.STATUT_JP_CAPTURE)
        .select_related('client', 'produit', 'variante')
    )

    for commande in commandes:
        # Une expiration précédente dans cette même passe a pu confirmer/avancer la file.
        commande.refresh_from_db()
        if commande.statut != Commande.STATUT_JP_CAPTURE:
            continue

        # Seuls les clients ÉLIGIBLES (en tête, avec du stock) sont relancés.
        if not _order_is_eligible(commande):
            continue

        last_message = commande.messages.order_by('-date_envoi').first()
        if not last_message:
            continue

        if not force and last_message.date_envoi + timedelta(minutes=delay) > now:
            continue

        relances_envoyees = commande.messages.aggregate(m=Max('numero_relance'))['m'] or 0

        if relances_envoyees < max_relances:
            relance_num = relances_envoyees + 1
            rappel = pick([
                'Mbola miandry kely ny infos-nao izahay',
                'Mba mila ny infos-nao ihany izahay',
            ])
            contenu = (
                f"{greeting(commande.client.nom)}! Fampahatsiahivana kely momba ny commande-nao "
                f"'{commande.produit.nom}'. {rappel} : anarana, numéro, adresse, daty sy ora ary "
                f'firy no alainao, azafady.'
            )
            from backend.order_messaging import send_relance_message

            outbound = send_relance_message(commande, contenu, numero_relance=relance_num)
            relances.append({
                'commande_id': commande.id,
                'numero_relance': relance_num,
                'delivery': outbound.get('delivery'),
            })
            logger.info(
                'Relance #%s envoyée pour commande #%s (sent=%s)',
                relance_num,
                commande.id,
                (outbound.get('delivery') or {}).get('sent'),
            )
        else:
            expire_commande(commande)
            expirations.append(commande.id)
            logger.info(
                'Commande #%s expirée (max relances atteint) — place libérée',
                commande.id,
            )

    return {
        'relances': relances,
        'expirations': expirations,
        'inbox_synced': inbox_count,
        'relances_count': len(relances),
        'expirations_count': len(expirations),
    }


class _JpRelanceScheduler(threading.Thread):
    daemon = True

    def __init__(self, interval_seconds: float):
        super().__init__(name='azlive-jp-relances')
        self._interval = max(15.0, float(interval_seconds))
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        msg = (
            f'Planificateur relances JP démarré '
            f'(toutes les {int(self._interval)}s, délai={getattr(settings, "AZLIVE_JP_RELANCE_DELAY_MINUTES", 30)} min, '
            f'max={getattr(settings, "AZLIVE_JP_MAX_RELANCES", 3)})'
        )
        logger.info(msg)
        try:
            print(f'\n[JP RELANCES] {msg}\n', flush=True)
        except UnicodeEncodeError:
            pass
        # Premier passage après un court délai : laisser Django finir le boot.
        if self._stop.wait(5):
            return
        while not self._stop.is_set():
            close_old_connections()
            try:
                result = process_jp_relances(force=False)
                if result['relances_count'] or result['expirations_count']:
                    logger.info(
                        'Relances auto : %s relance(s), %s expiration(s)',
                        result['relances_count'],
                        result['expirations_count'],
                    )
            except Exception:
                logger.exception('Erreur planificateur relances JP')
            finally:
                close_old_connections()
            if self._stop.wait(self._interval):
                break
        logger.info('Planificateur relances JP arrêté')


def start_jp_relance_scheduler() -> None:
    """Démarre le thread de relances auto (idempotent)."""
    if not getattr(settings, 'AZLIVE_JP_RELANCE_AUTO', True):
        logger.info('Relances JP auto désactivées (AZLIVE_JP_RELANCE_AUTO=false)')
        return

    interval = getattr(settings, 'AZLIVE_JP_RELANCE_POLL_SECONDS', 60)
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None and _scheduler.is_alive():
            return
        _scheduler = _JpRelanceScheduler(interval)
        _scheduler.start()


def stop_jp_relance_scheduler() -> None:
    global _scheduler
    with _scheduler_lock:
        if _scheduler is None:
            return
        _scheduler.stop()
        _scheduler = None
