"""Réponses automatiques pour les questions liées à la vente (prix, stock, lieu).
Assistance humaine uniquement pour les sujets hors flux automatisé (autre/inconnu)."""

import logging
import re
from typing import Any

from .ai import HybridCommentAnalyzer
from .models import Client, Commande, Vendeur
from .order_confirmation import _is_cancellation

logger = logging.getLogger(__name__)

# Intentions gérées automatiquement (achat, annulation, et maintenant prix/stock/lieu/salutation).
HANDLED_INTENTS = frozenset({'achat', 'annulation'})

# Ces intents reçoivent une réponse automatique du bot (pas d'escalade humaine).
AUTO_REPLY_INTENTS = frozenset({'question_prix', 'question_stock', 'lieu', 'salutation'})

# Seuls ces intents escaladent vers un humain.
HUMAN_ASSISTANCE_INTENTS = frozenset({'autre', 'inconnu'})

OFF_TOPIC_HINTS = re.compile(
    r'(ohatrinona|hoatrinona|prix|misy\s+ve|mbola\s+misy|disponib|tsy\s+misy|'
    r'afaka\s+ve|manao\s+ahoana|inona\s+ny|aiza|manao\s+fihaonana|misy\s+olona)',
    re.IGNORECASE,
)


def needs_auto_reply(analysis: dict | None) -> bool:
    """Renvoie True si le message mérite une réponse automatique du bot (prix/stock/lieu/salutation)."""
    if not analysis:
        return False
    intent = (analysis.get('intent') or '').lower()
    return intent in AUTO_REPLY_INTENTS


def needs_human_assistance(analysis: dict | None) -> bool:
    """Renvoie True uniquement pour les sujets vraiment hors flux (autre, inconnu)."""
    if not analysis:
        return False
    intent = (analysis.get('intent') or '').lower()
    if intent in HANDLED_INTENTS or intent in AUTO_REPLY_INTENTS:
        return False
    if intent in HUMAN_ASSISTANCE_INTENTS:
        return True
    # Texte non vide sans intention reconnue = escalade humaine
    return bool((analysis.get('raw_text') or '').strip())


def analyze_client_message(
    text: str,
    *,
    vendeur=None,
    live=None,
) -> dict:
    analyzer = HybridCommentAnalyzer()
    return analyzer.analyze(text, vendeur=vendeur, live=live)


def _looks_like_order_info(text: str, parsed: dict[str, str]) -> bool:
    cleaned = (text or '').strip()
    if OFF_TOPIC_HINTS.search(cleaned) or '?' in cleaned:
        return False
    if parsed:
        return True
    from .order_confirmation import (
        _is_quantity_line,
        _looks_like_date,
        _looks_like_phone,
        _looks_like_time,
    )

    lines = [line.strip() for line in (text or '').splitlines() if line.strip()]
    if not lines:
        return False
    if len(lines) == 1:
        line = lines[0]
        if _looks_like_phone(line) or _looks_like_date(line) or _looks_like_time(line):
            return True
        if _is_quantity_line(line):
            return True
        if len(line.split()) <= 3 and not OFF_TOPIC_HINTS.search(line):
            return True
    return False


def is_off_topic_private_message(text: str, parsed: dict[str, str] | None = None) -> bool:
    if _is_cancellation(text):
        return False
    if _looks_like_order_info(text, parsed or {}):
        return False
    cleaned = (text or '').strip()
    if not cleaned:
        return False
    if OFF_TOPIC_HINTS.search(cleaned):
        return True
    if '?' in cleaned:
        return True
    return len(cleaned.split()) >= 4 and not parsed


def find_commande_for_client(client: Client, vendeur: Vendeur | None = None) -> Commande | None:
    queryset = (
        Commande.objects.select_related('client', 'produit__vendeur')
        .filter(client=client)
        .order_by('-id')
    )
    if vendeur:
        queryset = queryset.filter(produit__vendeur=vendeur)
    return queryset.first()


def resolve_vendeur(
    *,
    vendeur: Vendeur | None = None,
    page_id: str | None = None,
    streamer_unique_id: str | None = None,
    commande: Commande | None = None,
) -> Vendeur | None:
    if vendeur:
        return vendeur
    if commande:
        return commande.produit.vendeur
    if page_id:
        from .jp_capture import resolve_vendeur_from_page

        return resolve_vendeur_from_page(page_id)
    if streamer_unique_id:
        from .jp_capture import resolve_vendeur_from_tiktok_username

        return resolve_vendeur_from_tiktok_username(streamer_unique_id)
    return None


def handle_human_assistance_request(
    *,
    client: Client,
    message_text: str,
    channel: str,
    vendeur: Vendeur | None = None,
    live=None,
    page_id: str | None = None,
    comment_id: str | None = None,
    commande: Commande | None = None,
    analysis: dict | None = None,
) -> dict[str, Any]:
    """Répond au client et prévient le vendeur qu'un humain doit reprendre la main."""
    from .order_messaging import (
        deliver_message_to_client,
        notify_vendeur_human_assistance,
        send_human_assistance_client_message,
    )

    vendeur = resolve_vendeur(
        vendeur=vendeur,
        page_id=page_id,
        commande=commande,
    )
    commande = commande or find_commande_for_client(client, vendeur=vendeur)

    if commande and message_text:
        from .models import Message

        Message.objects.create(
            commande=commande,
            contenu=message_text,
            numero_relance=0,
            direction=Message.DIRECTION_INBOUND,
            canal=channel,
        )

    client_outbound = send_human_assistance_client_message(
        client,
        commande=commande,
        comment_id=comment_id,
        page_id=page_id,
        canal=channel,
    )
    seller_alert = None
    if vendeur:
        seller_alert = notify_vendeur_human_assistance(
            vendeur,
            client,
            message_text,
            channel,
            analysis=analysis,
        )
    else:
        logger.warning(
            'Assistance humaine sans vendeur résolu — client %s, canal %s',
            client.id,
            channel,
        )

    return {
        'status': 'Assistance humaine demandée',
        'channel': channel,
        'client': {'id': client.id, 'nom': client.nom},
        'vendeur_id': vendeur.id if vendeur else None,
        'commande_id': commande.id if commande else None,
        'ai_analysis': analysis,
        'message_client': client_outbound.get('content'),
        'message_delivery': client_outbound.get('delivery'),
        'alerte_vendeur': seller_alert,
    }


def handle_human_assistance_from_comment(
    *,
    sender_id: str,
    sender_name: str,
    comment_text: str,
    channel: str,
    page_id: str | None = None,
    streamer_unique_id: str | None = None,
    vendeur=None,
    live=None,
    id_field: str = 'facebook_id',
    comment_id: str | None = None,
    analysis: dict | None = None,
) -> dict[str, Any]:
    if vendeur is None and page_id:
        from .jp_capture import resolve_vendeur_from_page

        vendeur = resolve_vendeur_from_page(page_id)
    if vendeur is None and channel == 'TikTok' and streamer_unique_id:
        from .jp_capture import resolve_vendeur_from_tiktok_username

        vendeur = resolve_vendeur_from_tiktok_username(streamer_unique_id)

    if analysis is None:
        analysis = analyze_client_message(comment_text, vendeur=vendeur, live=live)

    lookup = {id_field: sender_id}
    defaults = {'nom': sender_name, 'telephone': '', 'adresse': ''}
    client, created = Client.objects.get_or_create(**lookup, defaults=defaults)

    placeholder_names = {'Client Live', 'Client Facebook', 'Client TikTok'}
    if not created and client.nom in placeholder_names and sender_name not in placeholder_names:
        client.nom = sender_name
        client.save(update_fields=['nom'])

    result = handle_human_assistance_request(
        client=client,
        message_text=comment_text,
        channel=channel,
        vendeur=vendeur,
        live=live,
        page_id=page_id,
        comment_id=comment_id,
        analysis=analysis,
    )
    result['client_cree'] = created
    return result
