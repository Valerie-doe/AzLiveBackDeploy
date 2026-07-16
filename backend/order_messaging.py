import logging
from typing import Any

from django.conf import settings

from .facebook_messenger import send_facebook_private_message, send_facebook_private_reply
from .message_humanizer import emoji, first_name, greeting, pick, thanks, thanks_with_name
from .models import Commande, Message

logger = logging.getLogger(__name__)


def _public_base_url() -> str:
    return getattr(settings, 'AZLIVE_PUBLIC_BASE_URL', 'http://localhost:8000').rstrip('/')


def public_order_form_url(live_id: int) -> str:
    """Lien client du formulaire de confirmation pour un live."""
    base = getattr(settings, 'AZLIVE_PUBLIC_ORDER_BASE_URL', None) or getattr(
        settings, 'AZLIVE_PUBLIC_BASE_URL', 'http://localhost:3000'
    )
    return f'{str(base).rstrip("/")}/commander/{live_id}'


def _document_urls(commande_id: int) -> dict[str, str]:
    base = _public_base_url()
    return {
        'facture_url': f'{base}/api/commandes/{commande_id}/facture.pdf',
        'etiquette_url': f'{base}/api/commandes/{commande_id}/etiquette-livraison.pdf',
    }


def _detect_channel(commande: Commande) -> str:
    client = commande.client
    if client.facebook_id:
        return Message.CANAL_FACEBOOK
    if client.tiktok_id:
        return Message.CANAL_TIKTOK
    return Message.CANAL_MOCK


def _record_outbound(commande: Commande, content: str, canal: str, *, numero_relance: int = 0) -> Message:
    return Message.objects.create(
        commande=commande,
        contenu=content,
        numero_relance=numero_relance,
        direction=Message.DIRECTION_OUTBOUND,
        canal=canal,
    )


def _claim_messenger_psid(commande: Commande, delivery: dict[str, Any]) -> None:
    """Après private_reply, Meta renvoie souvent le PSID : on l'enregistre côté client.

    Plus besoin que le client clique un lien : les prochains MP partent directement
    via Send API, et la synchro inbox peut rattacher ses réponses.
    """
    psid = str(delivery.get('recipient_id') or '').strip()
    if not psid.isdigit():
        return

    from .models import Client

    client = commande.client
    current = str(client.facebook_id or '')
    if current == psid:
        return
    if current.isdigit() and current != psid:
        return
    if Client.objects.filter(facebook_id=psid).exclude(pk=client.pk).exists():
        logger.warning(
            'PSID %s déjà lié à un autre client — commande #%s non mise à jour',
            psid,
            commande.id,
        )
        return
    client.facebook_id = psid
    client.save(update_fields=['facebook_id'])
    logger.info('PSID Messenger %s rattaché au client #%s (commande #%s)', psid, client.id, commande.id)


def _sync_client_messenger_id(client, delivery: dict[str, Any]) -> None:
    """Enregistre le PSID Messenger renvoyé par l'API après une private_reply."""
    if not delivery.get('sent'):
        return
    recipient_id = delivery.get('recipient_id')
    if not recipient_id:
        return
    psid = str(recipient_id)
    if client.facebook_id != psid:
        logger.info(
            'Client #%s : facebook_id mis à jour %s -> %s (PSID Messenger)',
            client.pk,
            client.facebook_id,
            psid,
        )
        client.facebook_id = psid
        client.save(update_fields=['facebook_id'])


def _log_delivery_failure(commande: Commande | None, delivery: dict[str, Any], *, context: str) -> None:
    if delivery.get('sent'):
        return
    if delivery.get('mock'):
        return
    target = f'commande #{commande.id}' if commande else 'client'
    logger.warning(
        'Envoi Messenger échoué (%s, %s): %s',
        context,
        target,
        delivery.get('error') or delivery.get('detail') or 'erreur inconnue',
    )


def _deliver_private_message(
    commande: Commande,
    content: str,
    comment_id: str | None = None,
    *,
    numero_relance: int = 0,
) -> dict[str, Any]:
    canal = _detect_channel(commande)
    delivery = {'channel': canal, 'sent': False, 'mock': True}

    if canal == Message.CANAL_FACEBOOK and (commande.client.facebook_id or comment_id):
        from .order_confirmation import resolve_page_for_commande

        page = resolve_page_for_commande(commande)
        if page:
            if comment_id:
                # Réponse privée à un commentateur (live/post) : seul canal possible
                # car l'id du commentaire n'est pas un PSID Messenger.
                result = send_facebook_private_reply(page, comment_id, content)
                _claim_messenger_psid(commande, result)
            else:
                result = send_facebook_private_message(
                    page,
                    commande.client.facebook_id,
                    content,
                )
            delivery.update(result)
            delivery['mock'] = bool(result.get('mock', False))
            if not result.get('sent'):
                delivery['mock'] = True
            _log_delivery_failure(commande, delivery, context='jp_capture')

    elif canal == Message.CANAL_TIKTOK:
        # TikTok DM officiel indisponible — journaliser pour envoi manuel / WhatsApp futur
        logger.info(
            '[TIKTOK DM PENDING] commande #%s → @%s: %s',
            commande.id,
            commande.client.tiktok_id,
            content[:120],
        )
        delivery['detail'] = (
            'TikTok ne permet pas l\'envoi automatique de DM. '
            'Copiez le message depuis la console ou utilisez WhatsApp si le client a laissé son numéro.'
        )

    if delivery.get('mock', True):
        logger.info('[MESSAGING MOCK] commande #%s (%s): %s', commande.id, canal, content)
        print(f'\n [ORDER MESSAGING] Message privé ({canal}) commande #{commande.id}:')
        print(f'   > {content}\n')

    _record_outbound(commande, content, canal, numero_relance=numero_relance)
    return delivery


def build_jp_confirmation_message(commande: Commande) -> str:
    from .order_confirmation import _order_is_eligible

    client = commande.client
    produit = commande.produit
    hello = greeting(client.nom)

    if not _order_is_eligible(commande):
        intro = pick(
            [
                f"{hello} 😊 Voaray ny JP-nao ho an'ny '{produit.nom}'.",
                f"{hello}! Efa azonay ny JP-nao ho an'ny '{produit.nom}'.",
                f"{hello}! Tonga soa ny JP-nao ho an'ny '{produit.nom}'.",
            ]
        )
        attente = pick(
            [
                f"Fa efa misy nanao commande mialoha anao, ka ao amin'ny liste d'attente ianao aloha (numéro {commande.ordre_jp}).",
                f"Saingy mbola misy olona eo alohanao, ka miandry kely ianao izao (numéro {commande.ordre_jp} amin'ny liste d'attente).",
                f"Mbola eo am-piandrasana ny anjaranao ianao izao (numéro {commande.ordre_jp} amin'ny liste d'attente).",
            ]
        )
        rassurance = pick(
            [
                "Hilazanay anao raha vao misy toerana. Misaotra amin'ny faharetana!",
                "Raha vao misy malalaka dia tofandrenesinay anao. Misaotra e!",
                "Aza manahy, holazainay anao raha vao tonga ny anjaranao.",
            ]
        )
        return f'{intro} {attente} {rassurance}{emoji(prob=0.4)}'

    intro = pick(
        [
            f"{hello} 😊 Voaray ny JP-nao ho an'ny '{produit.nom}' (Commande #{commande.id}).",
            f"{hello}! Efa azonay ny JP-nao ho an'ny '{produit.nom}' (Commande #{commande.id}).",
            f"{hello}! Tonga soa ny JP-nao ho an'ny '{produit.nom}' (Commande #{commande.id}).",
        ]
    )
    demande = pick(
        [
            "Mba alefaso aminay azafady ny anaranao, numéro, adresse, daty sy ora "
            "hanaterana, ary firy no alainao.",
            "Mba hahavita ny commande, omeo anay ny anaranao, numéro, adresse, daty sy "
            "ora hanaterana, ary firy no alainao.",
            "Lazao anay azafady ny anaranao, numéro, adresse, daty sy ora hanaterana, "
            "ary firy no alainao.",
        ]
    )
    souplesse = pick(
        [
            "Afaka soratanao tsikelikely ihany, tsy maika, tsy misy modèle tsy maintsy arahina.",
            "Azonao zaraina amin'ny message maromaro, araka izay mora aminao.",
            "Ataovy mora fotsiny, tsy voatery atao indray miaraka.",
        ]
    )
    return f'{intro}\n\n{demande} {souplesse}{emoji(prob=0.3)}'


FIELD_COMPLETION_PROMPTS = {
    'nom': 'ny anaranao',
    'telephone': 'ny numéro-nao',
    'adresse': 'ny adresse-nao',
    'date_livraison': 'ny daty hanaterana',
    'heure_livraison': 'ny ora (ohatra 14h)',
    'quantite': 'firy no alainao (ohatra 2)',
}


def build_completion_request_message(commande: Commande, missing_fields: list[str]) -> str:
    from .order_confirmation import _collected_fields_snapshot

    snapshot = _collected_fields_snapshot(commande)
    received = []
    if snapshot.get('nom'):
        received.append(f"anarana ({snapshot['nom']})")
    if snapshot.get('telephone'):
        received.append(f"numéro ({snapshot['telephone']})")
    if snapshot.get('adresse'):
        received.append(f"adresse ({snapshot['adresse']})")
    if snapshot.get('date_livraison'):
        received.append(f"daty ({snapshot['date_livraison']})")
    if snapshot.get('heure_livraison'):
        received.append(f"ora ({snapshot['heure_livraison']})")
    if snapshot.get('quantite'):
        received.append(f"firy ({snapshot['quantite']})")

    missing_labels = [FIELD_COMPLETION_PROMPTS[field] for field in missing_fields if field in FIELD_COMPLETION_PROMPTS]
    intro = f'{thanks()}!'
    if received:
        recu_label = pick(['Efa voaray', 'Efa azonay', 'Voaray tsara'])
        intro += f' {recu_label} : {", ".join(received)}.'
    if missing_labels:
        manque_label = pick(['Mbola mila', 'Ny sisa ilaina', 'Mbola ilaina'])
        intro += f'\n{manque_label} : {", ".join(missing_labels)}.'
    cloture = pick(
        [
            "Azonao alefa amin'ny message manaraka, tsy maika.",
            "Andrasanay rehefa vonona ianao, soraty fotsiny eto.",
            "Azonao soratana tsikelikely ihany, araka izay mora aminao.",
        ]
    )
    intro += f'\n{cloture}{emoji(prob=0.3)}'
    return intro


def send_completion_request_message(commande: Commande, missing_fields: list[str]) -> dict[str, Any]:
    content = build_completion_request_message(commande, missing_fields)
    delivery = _deliver_private_message(commande, content)
    return {'content': content, 'delivery': delivery}


def build_waiting_with_info_message(commande: Commande) -> str:
    """Le client a tout fourni mais reste en liste d'attente (stock pas encore dispo)."""
    client = commande.client
    produit = commande.produit
    fn = first_name(client.nom)
    intro = pick(
        [
            f"{thanks_with_name(client.nom)}! Voaray daholo ny infos-nao ho an'ny '{produit.nom}'.",
            f"{greeting(client.nom)}! Azonay tsara ny infos rehetra momba ny '{produit.nom}'.",
            f"{greeting(client.nom)}! Feno daholo ny infos-nao ho an'ny '{produit.nom}'. {thanks()}!",
        ]
    )
    attente = pick(
        [
            f"Fa mbola misy olona eo alohanao izao (numéro {commande.ordre_jp} amin'ny liste d'attente).",
            f"Mbola miandry ny anjaranao ihany ianao (numéro {commande.ordre_jp}).",
        ]
    )
    rassurance = pick(
        [
            "Raha vao misy toerana dia confirmé-nay ny commande-nao ary lazainay aminao. Misaotra amin'ny faharetana!",
            "Hovitainay avy hatrany ny commande-nao raha vao tonga ny anjaranao. Misaotra amin'ny fandeferana!",
        ]
    )
    return f'{intro} {attente} {rassurance}{emoji(prob=0.4)}'


def send_waiting_with_info_message(commande: Commande) -> dict[str, Any]:
    content = build_waiting_with_info_message(commande)
    delivery = _deliver_private_message(commande, content)
    return {'content': content, 'delivery': delivery}


def build_public_form_spot_available_message(commande: Commande) -> str:
    """Place libérée pour un client TikTok : il doit reouvrir le lien /commander/."""
    client = commande.client
    produit = commande.produit
    link = public_order_form_url(commande.live_id) if commande.live_id else ''
    intro = pick(
        [
            f"{greeting(client.nom)}! Vaovao tsara: misy toerana malalaka ho an'ny '{produit.nom}'.",
            f"{thanks()} {first_name(client.nom) or client.nom}! Afaka confirmé-na ny commande-nao '{produit.nom}' izao.",
        ]
    )
    action = (
        f"Sokafy indray ity rohy confirmation ity mba hanamafisana : {link}"
        if link
        else "Sokafy indray ny rohy confirmation nomen'ny mpividy mba hanamafisana ny commande-nao."
    )
    return f'{intro} {action}{emoji(prob=0.4)}'


def send_public_form_spot_available_message(commande: Commande) -> dict[str, Any]:
    content = build_public_form_spot_available_message(commande)
    delivery = _deliver_private_message(commande, content)
    return {'content': content, 'delivery': delivery}


def build_stock_partial_offer_message(commande: Commande, available: int) -> str:
    """Propose de prendre le stock restant ou d'attendre un réassort."""
    client = commande.client
    produit = commande.produit
    requested = commande.quantite_effective
    intro = pick(
        [
            f"{greeting(client.nom)}! Voaray ny infos-nao ho an'ny '{produit.nom}'.",
            f"{thanks_with_name(client.nom)}! Azonay ny commande-nao '{produit.nom}'.",
        ]
    )
    situation = (
        f"Saingy {requested} no nangatahinao fa {available} ihany no sisa amin'izao."
    )
    choix = pick(
        [
            f"Tianao alaina ve ny {available} sisa, sa te-hiandry ianao raha vao misy indray?",
            f"Afaka maka ny {available} sisa ianao izao, na miandry ny stock vaovao. Inona no tianao?",
        ]
    )
    aide = (
        f"Valio fotsiny : « ekena {available} » / « oui » raha alainao, "
        f"na « miandry » raha te-hiandry."
    )
    return f'{intro} {situation} {choix}\n\n{aide}{emoji(prob=0.3)}'


def send_stock_partial_offer_message(commande: Commande, available: int) -> dict[str, Any]:
    content = build_stock_partial_offer_message(commande, available)
    delivery = _deliver_private_message(commande, content)
    return {'content': content, 'delivery': delivery}


def build_promotion_completion_message(commande: Commande, missing_fields: list[str]) -> str:
    """Une place s'est libérée : le client est promu mais il manque encore des infos."""
    client = commande.client
    produit = commande.produit
    labels = [FIELD_COMPLETION_PROMPTS[field] for field in missing_fields if field in FIELD_COMPLETION_PROMPTS]
    # Ancres testées : « toerana malalaka » et « alefaso ».
    bonne_nouvelle = pick(['vaovao tsara', 'vaovao mahafaly', 'fa misy vaovao'])
    message = (
        f"{greeting(client.nom)}, {bonne_nouvelle}! Nisy toerana malalaka ho an'ny '{produit.nom}', "
        f"ka afaka manohy ny commande-nao ianao izao."
    )
    if labels:
        message += f"\nMba alefaso haingana azafady : {', '.join(labels)} mba hahavitanay azy.{emoji(prob=0.4)}"
    return message


def send_promotion_completion_message(commande: Commande, missing_fields: list[str]) -> dict[str, Any]:
    content = build_promotion_completion_message(commande, missing_fields)
    delivery = _deliver_private_message(commande, content)
    return {'content': content, 'delivery': delivery}


def build_thank_you_message(commande: Commande, *, promoted: bool = False) -> str:
    urls = _document_urls(commande.id)
    client = commande.client
    produit = commande.produit
    delivery_slot = ''
    if client.date_livraison_preferee:
        delivery_slot = client.date_livraison_preferee.strftime('%d/%m/%Y')
    if client.heure_livraison_preferee:
        hour_label = client.heure_livraison_preferee.strftime('%H:%M')
        delivery_slot = f'{delivery_slot} à {hour_label}'.strip()

    # Cas « promu » : le client était en liste d'attente, une place s'est libérée et
    # comme ses informations étaient déjà complètes, sa commande est prise en compte.
    # Ancre testée : « toerana malalaka ».
    if promoted:
        intro = pick(
            [
                f"{greeting(client.nom)}, vaovao tsara! Nisy toerana malalaka, ka vita sy "
                f"confirmé ny commande-nao '{produit.nom}' (#{commande.id}).",
                f"{greeting(client.nom)}, vaovao mahafaly! Nisy toerana malalaka, ka vita "
                f"ny commande-nao '{produit.nom}' (#{commande.id}).",
            ]
        )
    else:
        intro = pick(
            [
                f"{greeting(client.nom)}! Vita ny commande-nao '{produit.nom}' (#{commande.id}). {thanks()}!",
                f"{thanks_with_name(client.nom)}! Confirmé ny commande-nao '{produit.nom}' (#{commande.id}).",
                f"{thanks_with_name(client.nom)}! Vita tsara ny commande-nao "
                f"'{produit.nom}' (#{commande.id}).",
            ]
        )

    livraison = pick(
        [
            f"Ho avy ny livraison{(' ' + delivery_slot) if delivery_slot else ''}.",
            f"Haterinay ny entana{(' ' + delivery_slot) if delivery_slot else ''}.",
        ]
    )
    return (
        f"{intro}{emoji(prob=0.5)}\n\n"
        f"Facture : {urls['facture_url']}\n\n"
        f"{livraison}"
    )


def send_jp_confirmation_message(
    commande: Commande,
    comment_id: str | None = None,
) -> dict[str, Any]:
    content = build_jp_confirmation_message(commande)
    delivery = _deliver_private_message(commande, content, comment_id=comment_id)
    return {'content': content, 'delivery': delivery}


def build_order_cancelled_message(commande: Commande) -> str:
    client = commande.client
    produit = commande.produit
    intro = pick(
        [
            f"Ekena {client.nom}, nofoanana ny commande-nao '{produit.nom}' (#{commande.id}).",
            f"Azo {client.nom}, nesorina ny commande-nao '{produit.nom}' (#{commande.id}).",
            f"Ekena tsara, voafoana ny commande-nao '{produit.nom}' (#{commande.id}).",
        ]
    )
    cloture = pick(
        [
            "Raha nisy diso na te-hanao commande vaovao ianao, valio « mbola te-hividy » eto. Misaotra!",
            "Raha mbola te-hividy ianao, soraty « reprendre » na « mbola te-hividy » eto. Misaotra e!",
        ]
    )
    return f'{intro} {cloture}'


def send_order_cancelled_message(commande: Commande) -> dict[str, Any]:
    content = build_order_cancelled_message(commande)
    delivery = _deliver_private_message(commande, content)
    return {'content': content, 'delivery': delivery}


def build_reprise_message(commande: Commande, *, ancienne_id: int, outcome: str) -> str:
    """Explique qu'on crée une nouvelle commande (sans reprendre la place des autres)."""
    client = commande.client
    produit = commande.produit
    intro = pick(
        [
            f"{greeting(client.nom)}! Azonay fa te-hanao indray ny '{produit.nom}'.",
            f"{thanks_with_name(client.nom)}! Te-hiverina amin'ny commande ianao.",
        ]
    )
    regle = (
        f"Ny commande #{ancienne_id} efa foana. Tsy azonay alaina indray ny toerana "
        f"nomena ny manaraka — ka manomboka commande vaovao #{commande.id} ianao."
    )
    if outcome == 'confirme':
        suite = pick(
            [
                "Mbola misy stock, ka confirmé avy hatrany ny commande vaovao.",
                "Soa ihany fa mbola misy, dia vita ny commande vaovao.",
            ]
        )
    elif outcome == 'attente':
        suite = (
            f"Saingy efa nomena ny manaraka ny stock, ka ao amin'ny liste d'attente "
            f"ianao (numéro {commande.ordre_jp})."
        )
    elif outcome == 'stock_partiel':
        suite = "Mbola misy sisa kely — jereo ny safidy manaraka (alaina ny sisa sa miandry)."
    elif outcome == 'recap':
        suite = "Jereo ny infos teo ambany ; raha mety, valio « eka » / « ok », na « hanova … » raha mila ovaina."
    else:
        suite = "Mba fenoy / hamafiso ny infos raha mbola ilaina."
    return f'{intro} {regle} {suite}{emoji(prob=0.35)}'


def send_reprise_message(
    commande: Commande,
    *,
    ancienne_id: int,
    outcome: str,
) -> dict[str, Any]:
    content = build_reprise_message(commande, ancienne_id=ancienne_id, outcome=outcome)
    delivery = _deliver_private_message(commande, content)
    return {'content': content, 'delivery': delivery}


def build_reprise_recap_message(commande: Commande) -> str:
    """Récapitule les infos connues et demande une confirmation explicite."""
    from .order_confirmation import _collected_fields_snapshot

    snapshot = _collected_fields_snapshot(commande)
    client = commande.client
    produit = commande.produit
    intro = pick(
        [
            f"{greeting(client.nom)}! Ireto ny infos efa fantatra ho an'ny '{produit.nom}' :",
            f"{thanks_with_name(client.nom)}! Voaray ireto ny infos-nao :",
        ]
    )
    lignes = []
    if snapshot.get('nom'):
        lignes.append(f"• Anarana : {snapshot['nom']}")
    if snapshot.get('telephone'):
        lignes.append(f"• Numéro : {snapshot['telephone']}")
    if snapshot.get('adresse'):
        lignes.append(f"• Adresse : {snapshot['adresse']}")
    if snapshot.get('date_livraison'):
        lignes.append(f"• Daty : {snapshot['date_livraison']}")
    if snapshot.get('heure_livraison'):
        lignes.append(f"• Ora : {snapshot['heure_livraison']}")
    if snapshot.get('quantite'):
        lignes.append(f"• Isa : {snapshot['quantite']}")
    recap = '\n'.join(lignes) if lignes else "• (tsy ampy ny infos)"
    suite = pick(
        [
            "Raha mety izany, valio fotsiny « eka » na « ok ». "
            "Raha te-hanova : « hanova adresse … » na « ovaina ny numéro … » ohatra.",
            "Mety ve ireo? Valio « eka » raha ekena, na « hanova … » raha mila ovaina.",
        ]
    )
    return f'{intro}\n\n{recap}\n\n{suite}{emoji(prob=0.35)}'


def send_reprise_recap_message(commande: Commande) -> dict[str, Any]:
    content = build_reprise_recap_message(commande)
    delivery = _deliver_private_message(commande, content)
    return {'content': content, 'delivery': delivery}


def build_order_expired_message(commande: Commande) -> str:
    client = commande.client
    produit = commande.produit
    intro = pick(
        [
            f"{greeting(client.nom)}, voafoana ny commande-nao '{produit.nom}' (#{commande.id}) "
            "satria tsy tonga tao anatin'ny fotoana ny infos ilaina, ka nomena ny manaraka ny toerana.",
            f"{greeting(client.nom)}, lany ny fotoana hamenoana ny infos ho an'ny commande "
            f"'{produit.nom}' (#{commande.id}), ka voatery nomena ny manaraka ny toerana.",
        ]
    )
    cloture = pick(
        [
            "Raha mbola liana ianao, valio fotsiny eto. Misaotra!",
            "Raha te-hanao commande indray ianao, soraty eto fotsiny dia hanampy anao izahay. Misaotra e!",
        ]
    )
    return f'{intro} {cloture}'


def send_order_expired_message(commande: Commande) -> dict[str, Any]:
    content = build_order_expired_message(commande)
    delivery = _deliver_private_message(commande, content)
    return {'content': content, 'delivery': delivery}


def build_vendor_confirmation_notification(commande: Commande) -> str:
    """Notification vendeur à la confirmation d'une commande : résumé + facture + ticket livreur."""
    client = commande.client
    produit = commande.produit
    urls = _document_urls(commande.id)
    variante = commande.variante
    variante_label = ''
    if variante:
        parts = [p for p in [variante.couleur, variante.taille] if p]
        parts_str = ', '.join(parts)
        variante_label = f' ({parts_str})' if parts else ''

    prix_total = commande.get_prix_total()
    qty = commande.quantite_effective

    lines = [
        f" Commande #{commande.id} — {client.nom}",
        f"   Produit : {produit.nom}{variante_label} × {qty}",
        f"   Montant : {prix_total:,.0f} Ar",
        f"   Tél : {client.telephone or '—'}",
        f"   Adresse : {client.adresse or '—'}",
    ]
    if client.date_livraison_preferee:
        slot = client.date_livraison_preferee.strftime('%d/%m/%Y')
        if client.heure_livraison_preferee:
            slot += f" à {client.heure_livraison_preferee.strftime('%H:%M')}"
        lines.append(f"   Livraison : {slot}")
    lines += [
        f"   Facture : {urls['facture_url']}",
        f"   Ticket livreur : {urls['etiquette_url']}",
    ]
    return '\n'.join(lines)


def notify_vendeur_order_confirmed(vendeur, commande: Commande) -> dict[str, Any]:
    """Envoie la notification de confirmation au vendeur (log + impression future)."""
    content = build_vendor_confirmation_notification(commande)
    contact = vendeur.contact or 'contact inconnu'
    logger.info('[CONFIRMATION VENDEUR] %s (%s) : commande #%s', vendeur.nom, contact, commande.id)
    print(f'\n [CONFIRMATION VENDEUR] {vendeur.nom} ({contact})')
    print(f'{content}\n')
    return {'content': content, 'contact': contact, 'mock': True}


def send_order_confirmed_message(commande: Commande, *, promoted: bool = False) -> dict[str, Any]:
    """Envoie la facture au client ET notifie le vendeur (facture + ticket livreur)."""
    content = build_thank_you_message(commande, promoted=promoted)
    delivery = _deliver_private_message(commande, content)
    urls = _document_urls(commande.id)

    # Notification vendeur avec facture + ticket de livraison
    vendeur_alert = None
    try:
        vendeur = commande.produit.vendeur
        vendeur_alert = notify_vendeur_order_confirmed(vendeur, commande)
    except Exception as exc:  # noqa: BLE001
        logger.warning('Impossible de notifier le vendeur (commande #%s) : %s', commande.id, exc)

    return {
        'content': content,
        'delivery': delivery,
        'alerte_vendeur': vendeur_alert,
        **urls,
    }


def build_human_assistance_client_message(client) -> str:
    """Le client pose une question hors flux automatisé : on le rassure."""
    intro = pick(
        [
            f"{greeting(client.nom)}! Efa voaray ny hafatrao.",
            f"{greeting(client.nom)}! Azonay tsara ny message-nao.",
            f"Voaray ny hafatrao {first_name(client.nom) or 'tompoko'}.",
        ]
    )
    suite = pick(
        [
            'Misy olona avy amin\'ny ekipa hanampy anao tsy ho ela.',
            'Hisy hovalianao haingana, miandry kely fotsiny azafady.',
            'Efa nampita tamin\'ny ekipa izahay, hisy hamaly anao tsy ho ela.',
            'Mbola injainay kely, fa hisy olona hanampy anao haingana.',
        ]
    )
    return f'{intro} {suite}{emoji(prob=0.35)}'


def build_thanks_ack_message(commande: Commande | None = None, client=None) -> str:
    """Répond chaleureusement à un remerciement (Misaotra, Mankasitraka…)."""
    person = client or (commande.client if commande else None)
    prenom = first_name(person.nom) if person else ''
    who = prenom or 'tompoko'
    intro = pick(
        [
            f'Tsy misy fisaorana {who}!',
            f'Misaotra indrindra koa {who}!',
            f'Tsy maninona {who}, izahay no misaotra!',
            f'Mankasitraka {who}!',
        ]
    )
    if commande and commande.statut == Commande.STATUT_ANNULE:
        suite = pick(
            [
                'Efa voafoana ny commande. Raha mbola te-hividy ianao, soraty « mbola te-hividy ».',
                'Voaray ny fisaorana. Ny commande efa foana — eto foana izahay raha mila zavatra.',
            ]
        )
        return f'{intro} {suite}{emoji(prob=0.4)}'
    if commande and commande.statut == Commande.STATUT_CONFIRME:
        suite = pick(
            [
                'Efa confirmé ny commande-nao, miandry ny livraison fotsiny.',
                'Vonona ny livraison-nao izahay. Misaotra tamin\'ny fahatokisana!',
                'Haterinay ny entana araka ny daty voalaza. Misaotra e!',
            ]
        )
        return f'{intro} {suite}{emoji(prob=0.45)}'
    suite = pick(
        [
            'Eto foana izahay raha mbola misy ilaina.',
            'Azonao tohizana ny infos raha mbola tsy vita.',
            'Vonona hanampy anao foana izahay.',
        ]
    )
    return f'{intro} {suite}{emoji(prob=0.4)}'


def send_thanks_ack_message(commande: Commande | None = None, client=None) -> dict[str, Any]:
    target = commande
    if target is None and client is not None:
        # Dernière commande (y compris annulée) pour coller au contexte du fil.
        target = (
            Commande.objects.filter(client=client)
            .order_by('-date_creation')
            .first()
        )
    if target is None:
        content = build_thanks_ack_message(client=client)
        return {'content': content, 'delivery': {'sent': False, 'mock': True}}
    content = build_thanks_ack_message(commande=target, client=client or target.client)
    delivery = _deliver_private_message(target, content)
    return {'content': content, 'delivery': delivery}


def build_modification_ack_message(
    commande: Commande,
    changed_fields: list[str],
    *,
    prompt_details: bool = False,
) -> str:
    client = commande.client
    if prompt_details:
        return (
            f"{greeting(client.nom)}! Azonao ovaina ny infos. "
            f"Ohatra : « hanova adresse Ivato », « ovaina ny numéro 034… », "
            f"« hanova daty zoma maraina », « hanova firy 2 ».{emoji(prob=0.3)}"
        )
    labels = ', '.join(changed_fields) if changed_fields else 'ny infos'
    intro = pick(
        [
            f"{thanks_with_name(client.nom)}! Voaova ny {labels}.",
            f"Ekena {first_name(client.nom) or 'tompoko'}! Nosoloina ny {labels}.",
            f"{greeting(client.nom)}! Vita ny fanovana ({labels}).",
        ]
    )
    suite = pick(
        [
            f"Commande #{commande.id} nohavaozina. Misaotra!",
            f"Voarakitra ny fanovana ho an'ny commande #{commande.id}.",
        ]
    )
    return f'{intro} {suite}{emoji(prob=0.35)}'


def send_modification_ack_message(
    commande: Commande,
    changed_fields: list[str],
    *,
    prompt_details: bool = False,
) -> dict[str, Any]:
    content = build_modification_ack_message(
        commande,
        changed_fields,
        prompt_details=prompt_details,
    )
    delivery = _deliver_private_message(commande, content)
    return {'content': content, 'delivery': delivery}


def build_modification_revert_message(commande: Commande, *, reprise: bool = False) -> str:
    client = commande.client
    intro = pick(
        [
            f"{thanks_with_name(client.nom)}! Voafafana ny fanovana vao hatao.",
            f"{greeting(client.nom)}! Tafaverina ny infos teo alohan'ny fanovana.",
        ]
    )
    if reprise:
        suite = "Jereo indray ny récap ci-dessous ; valio « eka » raha mety, na « hanova … » raha mila ovaina."
    else:
        suite = pick(
            [
                f"Ny commande #{commande.id} dia mbola velona.",
                "Tsy voafafa ny commande — ny fanovana ihany no foanana.",
            ]
        )
    return f'{intro} {suite}{emoji(prob=0.35)}'


def build_modification_revert_unavailable_message(commande: Commande) -> str:
    client = commande.client
    return (
        f"{greeting(client.nom)}! Tsy misy fanovana vao hatao ho foanana. "
        f"Raha te-hanova, soraty « hanova … ». "
        f"Raha te-hanafoana ny commande manontolo, soraty « foano » na « annuler ».{emoji(prob=0.3)}"
    )


def send_modification_revert_message(commande: Commande, *, reprise: bool = False) -> dict[str, Any]:
    content = build_modification_revert_message(commande, reprise=reprise)
    delivery = _deliver_private_message(commande, content)
    return {'content': content, 'delivery': delivery}


def send_modification_revert_unavailable_message(commande: Commande) -> dict[str, Any]:
    content = build_modification_revert_unavailable_message(commande)
    delivery = _deliver_private_message(commande, content)
    return {'content': content, 'delivery': delivery}


def send_relance_message(
    commande: Commande,
    content: str,
    *,
    numero_relance: int = 1,
) -> dict[str, Any]:
    """Relance réelle via Messenger (plus le mock MessagingService)."""
    delivery = _deliver_private_message(commande, content, numero_relance=numero_relance)
    return {'content': content, 'delivery': delivery}


# ---------------------------------------------------------------------------
# Réponses automatiques : prix, stock, lieu, salutation
# ---------------------------------------------------------------------------

def build_auto_reply_prix(client, *, produit=None, vendeur=None, live=None) -> str:
    """Réponse automatique à une question sur le prix.

    Le prix est toujours renseigné en BDD (champ obligatoire à la création du produit).
    On affiche donc le vrai prix — pour un produit précis ou la liste des produits du live.
    """
    from .models import Produit, Variante

    hello = greeting(client.nom)

    if produit:
        # Produit précis mentionné : on affiche la gamme de prix réels
        variantes = list(produit.variantes.order_by('prix_unitaire'))
        if variantes:
            prix_min = variantes[0].prix_unitaire
            prix_max = variantes[-1].prix_unitaire
            if prix_min == prix_max:
                prix_label = f"{prix_min:,.0f} Ar"
            else:
                prix_label = f"{prix_min:,.0f} – {prix_max:,.0f} Ar"
            sujet = pick([
                f"{hello}! Ny vidiny ho an'ny '{produit.nom}' : {prix_label}.",
                f"{hello}! '{produit.nom}' : {prix_label}.",
            ])
        else:
            sujet = f"{hello}! Jereo ny vidiny amin'ny live."
    else:
        # Aucun produit précis : on liste les prix de tous les produits du live/vendeur
        produits_qs = Produit.objects.prefetch_related('variantes')
        if live is not None and live.produits_dressing.exists():
            produits_qs = produits_qs.filter(id__in=live.produits_dressing.values_list('id', flat=True))
        elif vendeur is not None:
            produits_qs = produits_qs.filter(vendeur=vendeur)

        lignes_prix = []
        for p in produits_qs[:8]:  # max 8 produits pour ne pas surcharger le message
            variantes = list(p.variantes.order_by('prix_unitaire'))
            if not variantes:
                continue
            prix_min = variantes[0].prix_unitaire
            prix_max = variantes[-1].prix_unitaire
            if prix_min == prix_max:
                lignes_prix.append(f"• {p.nom} : {prix_min:,.0f} Ar")
            else:
                lignes_prix.append(f"• {p.nom} : {prix_min:,.0f} – {prix_max:,.0f} Ar")

        if lignes_prix:
            sujet = f"{hello}! Ny vidiny amin'ny live :\n" + '\n'.join(lignes_prix)
        else:
            sujet = f"{hello}! Ny vidiny dia voasoratra amin'ny live."

    invite = pick([
        "Soraty 'JP [entana]' raha te-hividy.",
        "Azonao alefa ny commande amin'ny 'JP [entana]' raha vonona.",
        "Manoraty 'JP [entana]' raha te-hividy, hisy hamaly anao haingana.",
    ])
    return f'{sujet}\n{invite}{emoji(prob=0.3)}'


def build_auto_reply_stock(client, *, produit=None, vendeur=None) -> str:
    """Réponse automatique à une question sur la disponibilité."""
    hello = greeting(client.nom)

    if produit:
        stock_total = produit.stock_total
        if stock_total > 0:
            dispo = pick([
                f"Eny, mbola misy ny '{produit.nom}' ({stock_total} sisa).",
                f"Mbola misy ny '{produit.nom}', fa mandehana haingana!",
                f"Mbola available ny '{produit.nom}' — {stock_total} no sisa.",
            ])
        else:
            dispo = pick([
                f"Miala tsiny, lany ny '{produit.nom}' amin'izao fotoana izao.",
                f"Voafaritra ny '{produit.nom}' — tsy misy intsony izao.",
                f"Lany ny stock ho an'ny '{produit.nom}'. Jereo ny entana hafa!",
            ])
    else:
        dispo = pick([
            f"{hello}! Ny entana aseho mandritra ny live no mbola misy.",
            f"{hello}! Ny sisa stock dia voasoratra amin'ny live.",
            f"{hello}! Aseho ny stock mandritra ny live.",
        ])

    invite = pick([
        "Manoraty 'JP [entana]' raha te-hividy.",
        "Azonao alefa ny JP raha vonona ianao.",
        "Soraty 'JP [entana]' haingana raha mbola misy!",
    ])
    return f'{hello}! {dispo} {invite}{emoji(prob=0.3)}'


def build_auto_reply_lieu(client, *, vendeur=None) -> str:
    """Réponse automatique à une question sur le lieu / la livraison."""
    hello = greeting(client.nom)

    lieu_info = ''
    if vendeur and getattr(vendeur, 'contact', ''):
        lieu_info = f" — {vendeur.contact}"

    livraison = pick([
        f"{hello}! Manao livraison izahay{lieu_info}.",
        f"{hello}! Misy livraison any aminareo{lieu_info}.",
        f"{hello}! Ateriny any aminao ny entana{lieu_info}.",
    ])
    details = pick([
        "Afaka asiana ny adresse-nao rehefa manao commande.",
        "Lazao ny adresse-nao amin'ny fotoana fanamafisana ny commande.",
        "Ny adresse no ilaina amin'ny fanamarinana ny commande.",
    ])
    invite = pick([
        "Soraty 'JP [entana]' raha te-hividy.",
        "Manoraty 'JP [entana]' raha vonona.",
    ])
    return f'{livraison} {details} {invite}{emoji(prob=0.3)}'


def build_auto_reply_salutation(client) -> str:
    """Réponse automatique à une salutation."""
    hello = greeting(client.nom)
    bienvenue = pick([
        f"{hello}! Tongasoa amin'ny live-nay!",
        f"{hello}! Tsara nahita anao eto!",
        f"{hello}! Manao ahoana? Tongasoa!",
    ])
    invite = pick([
        "Soraty 'JP [entana]' raha te-hividy entana.",
        "Azonao jerena ny entana rehetra aseho amin'ny live.",
        "Manoraty 'JP [entana]' raha liana amin'ny zavatra aseho.",
    ])
    return f'{bienvenue} {invite}{emoji(prob=0.5)}'


def build_auto_reply_message(client, intent: str, *, produit=None, vendeur=None, live=None) -> str:
    """Dispatch vers le bon builder selon l'intention détectée."""
    if intent == 'question_prix':
        return build_auto_reply_prix(client, produit=produit, vendeur=vendeur, live=live)
    if intent == 'question_stock':
        return build_auto_reply_stock(client, produit=produit, vendeur=vendeur)
    if intent == 'lieu':
        return build_auto_reply_lieu(client, vendeur=vendeur)
    # salutation (ou fallback)
    return build_auto_reply_salutation(client)


def send_auto_reply_message(
    client,
    intent: str,
    *,
    produit=None,
    vendeur=None,
    live=None,
    commande=None,
    comment_id: str | None = None,
    page_id: str | None = None,
    canal: str | None = None,
) -> dict[str, Any]:
    """Envoie une réponse automatique et la journalise."""
    content = build_auto_reply_message(client, intent, produit=produit, vendeur=vendeur, live=live)
    delivery = deliver_message_to_client(
        client,
        content,
        canal=canal,
        comment_id=comment_id,
        page_id=page_id,
        commande=commande,
    )
    return {'content': content, 'delivery': delivery}


def build_human_assistance_seller_notification(
    client,
    message_text: str,
    channel: str,
    *,
    analysis: dict | None = None,
) -> str:
    """Alerte vendeur en malgache courant."""
    nom = client.nom or 'Client'
    extrait = (message_text or '').strip()
    if len(extrait) > 120:
        extrait = f'{extrait[:117]}...'

    intent = (analysis or {}).get('intent', '')
    if intent == 'question_prix':
        motif = pick(['manontany ny vidiny', 'milaza hoe te-hahalala ny vidiny'])
    elif intent == 'question_stock':
        motif = pick(['manontany raha mbola misy', 'milaza hoe te-hahalala raha mbola misy'])
    else:
        motif = pick(
            [
                'mila fanampiana olona',
                'manontany zavatra mila valiny mivantana',
                'milaza zavatra tsy azon\'ny robot valiana',
            ]
        )

    return pick(
        [
            f"Fanairana : {nom} {motif} ({channel}). Hafatra : « {extrait} »",
            f"{nom} {motif}. Jereo fa valio izy haingana — « {extrait} » ({channel})",
            f"Mila mpanampy olona i {nom} : « {extrait} » ({channel})",
        ]
    )


def _resolve_page_for_delivery(
    *,
    commande: Commande | None = None,
    page_id: str | None = None,
):
    if commande is not None:
        from .order_confirmation import resolve_page_for_commande

        return resolve_page_for_commande(commande)
    if page_id:
        from .models import PageFacebook

        return PageFacebook.objects.filter(page_id=str(page_id)).first()
    return None


def deliver_message_to_client(
    client,
    content: str,
    *,
    canal: str | None = None,
    comment_id: str | None = None,
    page_id: str | None = None,
    commande: Commande | None = None,
) -> dict[str, Any]:
    """Envoie un message privé au client, avec ou sans commande active."""
    if commande is not None:
        return _deliver_private_message(commande, content, comment_id=comment_id)

    canal = canal or (
        Message.CANAL_FACEBOOK
        if client.facebook_id
        else Message.CANAL_TIKTOK
        if client.tiktok_id
        else Message.CANAL_MOCK
    )
    delivery = {'channel': canal, 'sent': False, 'mock': True}
    page = _resolve_page_for_delivery(page_id=page_id)

    if canal == Message.CANAL_FACEBOOK and (client.facebook_id or comment_id):
        if page:
            if comment_id:
                result = send_facebook_private_reply(page, comment_id, content)
            else:
                result = send_facebook_private_message(page, client.facebook_id, content)
            delivery.update(result)
            delivery['mock'] = False
            _sync_client_messenger_id(client, delivery)
            _log_delivery_failure(None, delivery, context='client_message')

    elif canal == Message.CANAL_TIKTOK:
        logger.info(
            '[TIKTOK DM PENDING] client #%s → @%s: %s',
            client.id,
            client.tiktok_id,
            content[:120],
        )
        delivery['detail'] = (
            'TikTok ne permet pas l\'envoi automatique de DM. '
            'Copiez le message depuis la console ou utilisez WhatsApp si le client a laissé son numéro.'
        )

    if delivery.get('mock', True):
        logger.info('[MESSAGING MOCK] client #%s (%s): %s', client.id, canal, content)
        print(f'\n [ORDER MESSAGING] Message privé ({canal}) client #{client.id}:')
        print(f'   > {content}\n')

    return delivery


def notify_vendeur_human_assistance(
    vendeur,
    client,
    message_text: str,
    channel: str,
    *,
    analysis: dict | None = None,
) -> dict[str, Any]:
    content = build_human_assistance_seller_notification(
        client,
        message_text,
        channel,
        analysis=analysis,
    )
    contact = vendeur.contact or 'contact inconnu'
    logger.info('[ALERTE VENDEUR] %s (%s) : %s', vendeur.nom, contact, content)
    print(f'\n [ALERTE VENDEUR] {vendeur.nom} ({contact})')
    print(f'   > {content}\n')
    return {'content': content, 'contact': contact, 'mock': True}


def send_human_assistance_client_message(
    client,
    *,
    commande: Commande | None = None,
    comment_id: str | None = None,
    page_id: str | None = None,
    canal: str | None = None,
) -> dict[str, Any]:
    content = build_human_assistance_client_message(client)
    delivery = deliver_message_to_client(
        client,
        content,
        canal=canal,
        comment_id=comment_id,
        page_id=page_id,
        commande=commande,
    )
    if commande is not None:
        _record_outbound(commande, content, delivery.get('channel') or canal or Message.CANAL_MOCK)
    return {'content': content, 'delivery': delivery}
