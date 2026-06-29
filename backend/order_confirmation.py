import re
import unicodedata
from datetime import date, datetime, time
from typing import Any

from django.db import transaction
from django.utils import timezone

from .models import Client, Commande, Message, Paiement, PageFacebook, Vendeur
from .serializers import CommandeSerializer


class OrderConfirmationError(Exception):
    def __init__(self, message, status_code=400, payload=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload or {}


FIELD_PATTERNS = {
    'nom': re.compile(r'(?:^|\n)\s*(?:nom|anarana)\s*[:\-]\s*(.+)', re.IGNORECASE),
    'telephone': re.compile(r'(?:^|\n)\s*(?:tel(?:éphone)?|finday|phone)\s*[:\-]\s*(.+)', re.IGNORECASE),
    'adresse': re.compile(r'(?:^|\n)\s*(?:adres(?:se)?|adiresy)\s*[:\-]\s*(.+)', re.IGNORECASE),
    'date_livraison': re.compile(
        r'(?:^|\n)\s*(?:date(?:\s+livraison)?|daty)\s*[:\-]\s*(.+)',
        re.IGNORECASE,
    ),
    'heure_livraison': re.compile(
        r'(?:^|\n)\s*(?:heure|ora|time)\s*[:\-]\s*(.+)',
        re.IGNORECASE,
    ),
}

PHONE_PATTERN = re.compile(
    r'^(?:\+261[\s.-]?|0)(3[0-9]{2})[\s.-]?(\d{2})[\s.-]?(\d{3})[\s.-]?(\d{2})$'
)
PHONE_LOOSE_PATTERN = re.compile(r'(?:\+261|0)?3[0-9]{8}')

TIME_PATTERN = re.compile(
    r'^(\d{1,2})\s*[hH:]\s*(\d{2})?(?:\s*(?:min|ora))?$|^\d{1,2}:\d{2}$',
)

FRENCH_MONTHS = {
    'janvier': 1,
    'fevrier': 2,
    'mars': 3,
    'avril': 4,
    'mai': 5,
    'juin': 6,
    'juillet': 7,
    'aout': 8,
    'septembre': 9,
    'octobre': 10,
    'novembre': 11,
    'decembre': 12,
}


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize('NFKD', value.lower())
    return ''.join(ch for ch in normalized if not unicodedata.combining(ch))


def _normalize_phone(value: str) -> str | None:
    digits = re.sub(r'\D', '', value or '')
    if digits.startswith('261') and len(digits) >= 12:
        digits = '0' + digits[3:]
    if len(digits) == 9 and digits.startswith('3'):
        digits = '0' + digits
    if len(digits) == 10 and digits.startswith('03'):
        return digits
    return None


def _looks_like_phone(value: str) -> bool:
    return _normalize_phone(value) is not None


def _parse_delivery_time(value: str | None) -> time | None:
    if not value:
        return None
    cleaned = value.strip().lower().replace('h30', 'h30').replace(' ', '')
    match = re.match(r'^(\d{1,2})[h:](\d{2})$', cleaned)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return time(hour, minute)
    match = re.match(r'^(\d{1,2})[hH]$', value.strip())
    if match:
        hour = int(match.group(1))
        if 0 <= hour <= 23:
            return time(hour, 0)
    try:
        return datetime.strptime(value.strip(), '%H:%M').time()
    except ValueError:
        return None


def _looks_like_time(value: str) -> bool:
    return _parse_delivery_time(value) is not None


def _parse_french_date(value: str, reference: date | None = None) -> date | None:
    reference = reference or timezone.localdate()
    cleaned = value.strip()
    normalized = _normalize_text(cleaned)

    for fmt in ('%d/%m/%Y', '%d-%m-%Y', '%Y-%m-%d', '%d/%m/%y'):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue

    match = re.match(r'^(\d{1,2})\s+([a-z]+)(?:\s+(\d{4}))?$', normalized)
    if match:
        day = int(match.group(1))
        month = FRENCH_MONTHS.get(match.group(2))
        year = int(match.group(3)) if match.group(3) else reference.year
        if month and 1 <= day <= 31:
            try:
                parsed = date(year, month, day)
                if not match.group(3) and parsed < reference:
                    parsed = date(year + 1, month, day)
                return parsed
            except ValueError:
                return None
    return None


def _looks_like_date(value: str) -> bool:
    return _parse_french_date(value) is not None


def _extract_inline_date_time(value: str) -> tuple[str | None, str | None]:
    """Extrait date/heure d'une ligne mixte, ex. '12 mai 14h'."""
    remaining = value.strip()
    date_part = None
    time_part = None

    time_match = re.search(r'(\d{1,2}\s*[hH:]\s*\d{0,2})', remaining)
    if time_match:
        time_part = time_match.group(1).strip()
        remaining = remaining.replace(time_match.group(0), ' ').strip()

    if remaining and _looks_like_date(remaining):
        date_part = remaining

    return date_part, time_part


def parse_confirmation_text(text: str) -> dict[str, str]:
    """
    Extrait nom, téléphone, adresse, date et heure depuis un message privé.
    Accepte les formats étiquetés ou libres, ex. :
      Lova
      Bypass
      12 mai
      14h
    """
    cleaned = (text or '').strip()
    if not cleaned:
        return {}

    parsed: dict[str, str] = {}
    for field, pattern in FIELD_PATTERNS.items():
        match = pattern.search(cleaned)
        if match:
            parsed[field] = match.group(1).strip().split('\n')[0].strip()

    if len(parsed) >= 3:
        return parsed

    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if not lines:
        return parsed

    classified = {'phones': [], 'dates': [], 'times': [], 'others': []}
    for line in lines:
        # Une ligne « quantité » (ex. « 2 », « 2 pcs ») est gérée à part (champ commande),
        # surtout pas comme un nom ou une adresse.
        if _is_quantity_line(line):
            continue
        inline_date, inline_time = _extract_inline_date_time(line)
        if inline_date:
            classified['dates'].append(inline_date)
            if inline_time:
                classified['times'].append(inline_time)
            continue
        if inline_time and not inline_date:
            classified['times'].append(inline_time)
            continue
        if _looks_like_phone(line):
            phone = _normalize_phone(line)
            if phone:
                classified['phones'].append(phone)
            continue
        if _looks_like_time(line):
            classified['times'].append(line)
            continue
        if _looks_like_date(line):
            classified['dates'].append(line)
            continue
        classified['others'].append(line)

    if classified['phones']:
        parsed.setdefault('telephone', classified['phones'][0])
    if classified['dates']:
        parsed.setdefault('date_livraison', classified['dates'][0])
    if classified['times']:
        parsed.setdefault('heure_livraison', classified['times'][0])

    others = classified['others']
    if others:
        parsed.setdefault('nom', others[0])
        if len(others) > 1:
            parsed.setdefault('adresse', ' '.join(others[1:]))
        elif len(others) == 1 and not parsed.get('adresse'):
            # Une seule ligne texte restante sans téléphone/date → probablement l'adresse/quartier
            if parsed.get('nom') and parsed.get('telephone') and parsed.get('date_livraison'):
                parsed.setdefault('adresse', others[0])
            elif parsed.get('nom') and (parsed.get('date_livraison') or parsed.get('telephone')):
                if not _looks_like_date(others[0]) and not _looks_like_phone(others[0]):
                    if parsed['nom'] == others[0] and len(lines) >= 2:
                        pass
                    else:
                        parsed.setdefault('adresse', others[0] if parsed.get('nom') != others[0] else '')

    # Cas typique Madagascar : Nom / Quartier / Date [/ Heure]
    if len(lines) >= 3 and not parsed.get('adresse'):
        if (
            parsed.get('nom')
            and parsed.get('date_livraison')
            and len(classified['others']) >= 2
        ):
            parsed['adresse'] = classified['others'][1]
        elif len(classified['others']) == 2 and parsed.get('date_livraison'):
            parsed.setdefault('nom', classified['others'][0])
            parsed.setdefault('adresse', classified['others'][1])
        elif len(classified['others']) == 1 and parsed.get('nom') and parsed.get('date_livraison'):
            # nom + date détectés, 1 ligne quartier restante
            for line in classified['others']:
                if line != parsed.get('nom'):
                    parsed.setdefault('adresse', line)

    # Reconstruction explicite 3 lignes : Nom / Adresse / Date
    if len(lines) == 3 and not parsed.get('telephone'):
        if _looks_like_date(lines[2]) and not _looks_like_phone(lines[1]):
            parsed['nom'] = lines[0]
            parsed['adresse'] = lines[1]
            parsed['date_livraison'] = lines[2]

    if len(lines) == 4 and not parsed.get('telephone'):
        if _looks_like_date(lines[2]) and _looks_like_time(lines[3]) and not _looks_like_phone(lines[1]):
            parsed['nom'] = lines[0]
            parsed['adresse'] = lines[1]
            parsed['date_livraison'] = lines[2]
            parsed['heure_livraison'] = lines[3]

    return parsed


def _parse_delivery_date(value: str | None):
    if not value:
        return None
    return _parse_french_date(value)


def detect_client_channel(client: Client) -> str:
    if client.facebook_id:
        return 'Facebook'
    if client.tiktok_id:
        return 'TikTok'
    return 'Inconnu'


def find_pending_commande(client: Client, vendeur: Vendeur | None = None) -> Commande | None:
    queryset = (
        Commande.objects.select_related('produit', 'produit__vendeur', 'client', 'variante', 'live')
        .filter(client=client, statut=Commande.STATUT_JP_CAPTURE)
        .order_by('ordre_jp', '-date_creation')
    )
    if vendeur:
        queryset = queryset.filter(produit__vendeur=vendeur)
    return queryset.first()


# Statuts à partir desquels un client peut encore annuler sa commande.
# (On exclut volontairement EN_LIVRAISON, LIVRE et ANNULE : trop tard ou déjà fait.)
CANCELLABLE_STATUSES = (
    Commande.STATUT_JP_CAPTURE,
    Commande.STATUT_CONFIRME,
    Commande.STATUT_PREPARE,
)


def find_cancellable_commande(client: Client, vendeur: Vendeur | None = None) -> Commande | None:
    """Dernière commande encore annulable du client (JP en attente, confirmée ou préparée)."""
    queryset = (
        Commande.objects.select_related('produit', 'produit__vendeur', 'client', 'variante', 'live')
        .filter(client=client, statut__in=CANCELLABLE_STATUSES)
        .order_by('-date_creation')
    )
    if vendeur:
        queryset = queryset.filter(produit__vendeur=vendeur)
    return queryset.first()


def resolve_page_for_commande(commande: Commande) -> PageFacebook | None:
    vendeur = commande.produit.vendeur
    if commande.live_id and commande.live.pages_facebook:
        for item in commande.live.pages_facebook:
            page = (
                PageFacebook.objects.filter(vendeur=vendeur, nom=item).first()
                or PageFacebook.objects.filter(vendeur=vendeur, page_id=str(item)).first()
            )
            if page and page.access_token:
                return page

    return (
        PageFacebook.objects.filter(vendeur=vendeur, statut=PageFacebook.STATUT_PRET)
        .exclude(access_token__isnull=True)
        .exclude(access_token='')
        .first()
    )


CANCELLATION_PATTERNS = [
    # Français
    re.compile(r'\bannul', re.IGNORECASE),
    re.compile(r'\bje\s+(?:ne\s+)?(?:veux|prends?|prend)\s+plus\b', re.IGNORECASE),
    re.compile(r'\bne\s+(?:veux|prends?|prend)\s+plus\b', re.IGNORECASE),
    re.compile(r'\bplus\s+besoin\b', re.IGNORECASE),
    re.compile(r'\bnon\s+merci\b', re.IGNORECASE),
    # Malagasy
    # tsy + verbe vouloir/prendre/acheter/avoir besoin (te, tia, mila, ila, maka, haka, mividy, hividy...)
    re.compile(r'\btsy\s+(?:te|tia|mila|ila|ilaiko|maka|haka|mividy|hividy|haiko)\b', re.IGNORECASE),
    re.compile(r'\bfoan[ao]\b', re.IGNORECASE),       # foana / foano = annuler
    re.compile(r'\besory\b', re.IGNORECASE),          # esory = enlève / retire
    re.compile(r'\bajanon[ay]\b', re.IGNORECASE),     # ajanony = arrête
    re.compile(r'\bavelao\b', re.IGNORECASE),         # avelao = laisse tomber
    re.compile(r'^\s*tsia\s*$', re.IGNORECASE),       # tsia = non (seul)
]


def _is_cancellation(text: str) -> bool:
    """Détecte une réponse de refus/annulation explicite (FR ou MG)."""
    cleaned = (text or '').strip()
    if not cleaned:
        return False
    return any(pattern.search(cleaned) for pattern in CANCELLATION_PATTERNS)


QUANTITY_LABELLED_PATTERN = re.compile(
    r'(?:quantit[eé]|qte|qty|nombre|isan?[\'’y]?)\s*[:=\-]?\s*(\d{1,3})',
    re.IGNORECASE,
)
QUANTITY_SUFFIX_PATTERN = re.compile(
    r'(\d{1,3})\s*(?:pcs?|pi[eè]ces?|unit[eé]s?|isa)\b',
    re.IGNORECASE,
)
QUANTITY_X_PATTERN = re.compile(r'(?:^|\s)x\s*(\d{1,3})\b|\b(\d{1,3})\s*x(?:\s|$)', re.IGNORECASE)
QUANTITY_STANDALONE_PATTERN = re.compile(r'^\s*(\d{1,3})\s*$')


def _parse_quantity(text: str, *, expecting: bool = False) -> int | None:
    """Extrait une quantité d'un message libre.

    Motifs explicites toujours acceptés : « quantité: 2 », « isa 2 », « 2 pcs », « x2 ».
    Un nombre seul (« 2 ») n'est interprété comme quantité que lorsqu'on l'attend
    (expecting=True), pour ne pas confondre avec un téléphone, une date ou une heure.
    """
    if not text:
        return None

    for pattern in (QUANTITY_LABELLED_PATTERN, QUANTITY_SUFFIX_PATTERN):
        match = pattern.search(text)
        if match and int(match.group(1)) > 0:
            return int(match.group(1))

    match = QUANTITY_X_PATTERN.search(text)
    if match:
        value = int(match.group(1) or match.group(2))
        if value > 0:
            return value

    if expecting:
        for line in (l.strip() for l in text.splitlines() if l.strip()):
            if _looks_like_phone(line) or _looks_like_time(line) or _looks_like_date(line):
                continue
            standalone = QUANTITY_STANDALONE_PATTERN.match(line)
            if standalone:
                value = int(standalone.group(1))
                if 0 < value <= 999:
                    return value
    return None


def _is_quantity_line(line: str) -> bool:
    """Vrai si la ligne ne porte qu'une quantité (« 2 », « 2 pcs », « quantité : 2 »).

    Sert à éviter qu'un nombre seul soit pris à tort pour un nom ou une adresse.
    """
    cleaned = (line or '').strip()
    if not cleaned:
        return False
    if _looks_like_phone(cleaned) or _looks_like_time(cleaned) or _looks_like_date(cleaned):
        return False
    return bool(
        QUANTITY_STANDALONE_PATTERN.match(cleaned)
        or QUANTITY_LABELLED_PATTERN.search(cleaned)
        or QUANTITY_SUFFIX_PATTERN.search(cleaned)
    )


def _order_is_eligible(commande: Commande) -> bool:
    """Vrai si la commande peut être confirmée maintenant (assez de stock, à son tour).

    Le stock courant de la variante reflète déjà les commandes confirmées (décrémentées).
    On ne compte donc que les JP encore en attente PLACÉS DEVANT (ordre_jp plus petit) :
    s'ils consomment déjà tout le stock, ce client reste en liste d'attente.
    """
    variante = commande._get_stock_variante()
    if not variante:
        return True

    remaining = variante.stock
    ahead = (
        Commande.objects.filter(
            produit=commande.produit,
            variante=commande.variante,
            statut=Commande.STATUT_JP_CAPTURE,
            ordre_jp__lt=commande.ordre_jp,
        )
        .exclude(pk=commande.pk)
    )
    qty_ahead = sum(c.quantite_effective for c in ahead)
    return qty_ahead + commande.quantite_effective <= remaining


def _ensure_paiement(commande: Commande) -> Paiement:
    """Crée le règlement par défaut (paiement à la livraison, non payé) si absent."""
    paiement, _ = Paiement.objects.get_or_create(
        commande=commande,
        defaults={
            'methode': Paiement.METHODE_LIVRAISON,
            'statut': Paiement.STATUT_NON_PAYE,
        },
    )
    return paiement


def _missing_confirmation_fields(commande: Commande) -> list[str]:
    client = commande.client
    missing = []
    if not client.nom or client.nom in {'Client Live', 'Client Facebook', 'Client TikTok'}:
        missing.append('nom')
    if not client.telephone:
        missing.append('telephone')
    if not client.adresse:
        missing.append('adresse')
    if not client.date_livraison_preferee:
        missing.append('date_livraison')
    if not client.heure_livraison_preferee:
        missing.append('heure_livraison')
    if commande.quantite is None:
        missing.append('quantite')
    return missing


def _client_snapshot(client: Client) -> dict[str, Any]:
    return {
        'nom': client.nom,
        'telephone': client.telephone,
        'adresse': client.adresse,
        'date_livraison_preferee': client.date_livraison_preferee,
        'heure_livraison_preferee': client.heure_livraison_preferee.strftime('%H:%M')
        if client.heure_livraison_preferee
        else None,
    }


def _apply_parsed_fields(client: Client, parsed_data: dict[str, str]) -> None:
    if parsed_data.get('nom'):
        client.nom = parsed_data['nom']
    if parsed_data.get('telephone'):
        client.telephone = _normalize_phone(parsed_data['telephone']) or parsed_data['telephone']
    if parsed_data.get('adresse'):
        client.adresse = parsed_data['adresse']
    delivery_date = _parse_delivery_date(parsed_data.get('date_livraison'))
    if delivery_date:
        client.date_livraison_preferee = delivery_date
    delivery_time = _parse_delivery_time(parsed_data.get('heure_livraison'))
    if delivery_time:
        client.heure_livraison_preferee = delivery_time


def analyze_confirmation_message(text: str, client: Client | None = None) -> dict[str, str]:
    from .ai import ConfirmationMessageAnalyzer

    return ConfirmationMessageAnalyzer().analyze(text, client=client)['fields']


@transaction.atomic
def handle_client_reply(
    commande: Commande,
    parsed_data: dict[str, str],
    *,
    inbound_text: str = '',
    canal: str | None = None,
) -> dict[str, Any]:
    """Enregistre ce que le client a envoyé ; confirme si complet, sinon demande le reste."""
    client = commande.client
    canal_message = canal or detect_client_channel(client)

    if inbound_text:
        Message.objects.create(
            commande=commande,
            contenu=inbound_text,
            numero_relance=0,
            direction=Message.DIRECTION_INBOUND,
            canal=canal_message,
        )

    # Réponse négative explicite : on annule la commande — y compris après confirmation
    # ou préparation. Le stock éventuellement décrémenté est restauré et le suivant de la
    # file est promu via Commande.save().
    if _is_cancellation(inbound_text):
        if commande.statut not in CANCELLABLE_STATUSES:
            raise OrderConfirmationError(
                f'La commande #{commande.id} ne peut plus être annulée '
                f'(statut : {commande.get_statut_display()}).',
                status_code=409,
            )

        commande.statut = Commande.STATUT_ANNULE
        commande.save(update_fields=['statut'])

        from .order_messaging import send_order_cancelled_message

        outbound = send_order_cancelled_message(commande)
        return {
            'status': 'Commande annulée',
            'annule': True,
            'complet': False,
            'commande': CommandeSerializer(commande).data,
            'client': _client_snapshot(client),
            'message_annulation': outbound.get('content'),
            'message_delivery': outbound.get('delivery'),
        }

    # Au-delà de l'annulation, la complétion d'infos ne concerne que les JP en attente.
    if commande.statut != Commande.STATUT_JP_CAPTURE:
        raise OrderConfirmationError(
            f'La commande #{commande.id} est déjà au statut {commande.get_statut_display()}.',
            status_code=409,
        )

    _apply_parsed_fields(client, parsed_data)

    client.save(
        update_fields=[
            'nom',
            'telephone',
            'adresse',
            'date_livraison_preferee',
            'heure_livraison_preferee',
        ],
    )

    # Quantité : demandée pendant la collecte (pas dans le JP). On n'accepte un nombre
    # « nu » que tant qu'on attend justement la quantité.
    if commande.quantite is None:
        quantite = _parse_quantity(inbound_text, expecting=True)
        if quantite:
            commande.quantite = quantite
            commande.save(update_fields=['quantite'])

    missing = _missing_confirmation_fields(commande)
    if missing:
        from .order_messaging import send_completion_request_message

        outbound = send_completion_request_message(commande, missing)
        return {
            'status': 'Informations partielles — complétez quand vous voulez',
            'complet': False,
            'champs_manquants': missing,
            'champs_recus': {k: v for k, v in _client_snapshot(client).items() if v},
            'parsed': parsed_data,
            'client': _client_snapshot(client),
            'message_relance': outbound.get('content'),
            'message_delivery': outbound.get('delivery'),
        }

    # Infos complètes, mais le client peut être en liste d'attente (stock insuffisant
    # pour lui pour l'instant) : on garde sa commande en attente, sans prendre de stock.
    # Il sera confirmé automatiquement quand ce sera son tour (voir promote_queue).
    if not _order_is_eligible(commande):
        from .order_messaging import send_waiting_with_info_message

        outbound = send_waiting_with_info_message(commande)
        return {
            'status': "En liste d'attente — informations enregistrées",
            'complet': False,
            'en_attente': True,
            'champs_manquants': [],
            'commande': CommandeSerializer(commande).data,
            'client': _client_snapshot(client),
            'parsed': parsed_data,
            'message_attente': outbound.get('content'),
            'message_delivery': outbound.get('delivery'),
        }

    return _finalize_confirmation(commande, parsed_data=parsed_data)


def _finalize_confirmation(
    commande: Commande,
    *,
    parsed_data: dict[str, str] | None = None,
    promoted: bool = False,
) -> dict[str, Any]:
    """Confirme la commande : statut CONFIRME (décrément stock via save) + règlement + message.

    promoted=True quand la confirmation vient d'une montée en file (une place s'est libérée
    et les informations du client étaient déjà complètes) : le message le signale.
    """
    commande.statut = Commande.STATUT_CONFIRME
    commande.save(update_fields=['statut'])
    paiement = _ensure_paiement(commande)

    from .order_messaging import send_order_confirmed_message

    outbound = send_order_confirmed_message(commande, promoted=promoted)

    return {
        'status': 'Commande confirmée',
        'complet': True,
        'commande': CommandeSerializer(commande).data,
        'reglement': {'methode': paiement.methode, 'statut': paiement.statut},
        'client': _client_snapshot(commande.client),
        'parsed': parsed_data or {},
        'message_remerciement': outbound.get('content'),
        'message_delivery': outbound.get('delivery'),
        'facture_url': outbound.get('facture_url'),
        'etiquette_url': outbound.get('etiquette_url'),
    }


@transaction.atomic
def expire_commande(commande: Commande) -> dict[str, Any] | None:
    """Expire un JP en tête de file resté incomplet trop longtemps.

    On annule la commande (ce qui, via Commande.save(), fait monter le suivant de la file —
    confirmé automatiquement s'il est déjà complet) puis on prévient le client expiré.
    """
    if commande.statut != Commande.STATUT_JP_CAPTURE:
        return None

    commande.statut = Commande.STATUT_ANNULE
    commande.save(update_fields=['statut'])

    from .order_messaging import send_order_expired_message

    outbound = send_order_expired_message(commande)
    return {
        'commande_id': commande.id,
        'message_expiration': outbound.get('content'),
        'message_delivery': outbound.get('delivery'),
    }


def promote_queue(produit, variante=None, exclude_pk=None) -> None:
    """Fait avancer la file d'attente d'une déclinaison après libération de stock/place.

    Confirme automatiquement les commandes suivantes qui sont à la fois ÉLIGIBLES (stock)
    et COMPLÈTES (toutes les infos + quantité fournies). Dès qu'on rencontre une commande
    éligible mais incomplète, on lui demande ce qui manque et on s'arrête (elle garde sa
    place tant qu'elle n'a pas répondu).
    """
    while True:
        queryset = (
            Commande.objects.select_related('client', 'produit', 'variante')
            .filter(produit=produit, variante=variante, statut=Commande.STATUT_JP_CAPTURE)
            .order_by('ordre_jp')
        )
        if exclude_pk:
            queryset = queryset.exclude(pk=exclude_pk)

        commande = queryset.first()
        if commande is None or not _order_is_eligible(commande):
            return

        missing = _missing_confirmation_fields(commande)
        if missing:
            # Place libérée mais infos incomplètes : on prévient et on demande ce qui manque.
            from .order_messaging import send_promotion_completion_message

            send_promotion_completion_message(commande, missing)
            return

        # Place libérée et infos déjà complètes : confirmation automatique (message dédié).
        _finalize_confirmation(commande, promoted=True)


@transaction.atomic
def confirm_commande_from_message(
    commande: Commande,
    parsed_data: dict[str, str],
    *,
    inbound_text: str = '',
    canal: str | None = None,
) -> dict[str, Any]:
    return handle_client_reply(
        commande,
        parsed_data,
        inbound_text=inbound_text,
        canal=canal,
    )


def process_inbound_private_message(
    *,
    sender_id: str,
    message_text: str,
    channel: str,
    page_id: str | None = None,
    id_field: str = 'facebook_id',
) -> dict[str, Any]:
    if not sender_id or not message_text:
        raise OrderConfirmationError('Message privé vide ou expéditeur manquant.')

    lookup = {id_field: sender_id}
    client = Client.objects.filter(**lookup).first()
    if not client:
        raise OrderConfirmationError(
            'Aucun client trouvé pour cet identifiant. Postez d\'abord un JP pendant le live.',
            status_code=404,
        )

    vendeur = None
    if page_id:
        page = PageFacebook.objects.select_related('vendeur').filter(page_id=str(page_id)).first()
        vendeur = page.vendeur if page else None

    # Pour une annulation, on cherche aussi les commandes déjà confirmées/préparées,
    # pas uniquement les JP encore en attente.
    if _is_cancellation(message_text):
        commande = find_pending_commande(client, vendeur=vendeur) or find_cancellable_commande(
            client, vendeur=vendeur
        )
        if not commande:
            raise OrderConfirmationError(
                'Aucune commande active à annuler pour ce client.',
                status_code=404,
            )
    else:
        commande = find_pending_commande(client, vendeur=vendeur)
        if not commande:
            raise OrderConfirmationError(
                'Aucune commande JP en attente de confirmation pour ce client.',
                status_code=404,
            )

    parsed = analyze_confirmation_message(message_text, client=client)
    return handle_client_reply(
        commande,
        parsed,
        inbound_text=message_text,
        canal=channel,
    )
