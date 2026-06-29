"""Couche de service LLM : analyse haut-niveau avec repli (fallback) regex.

- Si le LLM est disponible : Gemini comprend le message (extraction structuree).
  Pour les commentaires, on lui fournit le CATALOGUE produits afin qu'il choisisse
  directement la bonne variante (bien plus fiable sur du texte mixte mg/fr).
- Sinon (cle absente, quota, panne) : repli AUTOMATIQUE sur les analyseurs regex
  existants (`backend.ai`, `backend.order_confirmation`).

Lecture seule : aucune ecriture en base, le flux de commandes existant n'est pas
touche.
"""
import logging

from django.utils import timezone

from . import client
from .prompts import (
    COMMENT_SYSTEM,
    CONFIRMATION_SYSTEM,
    build_comment_prompt,
    build_confirmation_prompt,
)

logger = logging.getLogger(__name__)

REQUIRED_DELIVERY_FIELDS = ['nom', 'telephone', 'adresse', 'date_livraison', 'heure_livraison']
CATALOG_LIMIT = 300


def _catalog_lines(vendeur_id=None, live_id=None, limit=CATALOG_LIMIT):
    """Construit la liste des variantes disponibles, optionnellement filtree.

    Filtrage prioritaire : live (dressing) > vendeur > tout.
    """
    from ..models import Live, Variante

    queryset = Variante.objects.select_related('produit')

    if live_id:
        live = Live.objects.filter(id=live_id).first()
        if live:
            produit_ids = list(live.produits_dressing.values_list('id', flat=True))
            if produit_ids:
                queryset = queryset.filter(produit_id__in=produit_ids)
    elif vendeur_id:
        queryset = queryset.filter(produit__vendeur_id=vendeur_id)

    lines = []
    for variante in queryset[:limit]:
        lines.append(
            f"- id={variante.id} | code={variante.code_jp} | produit={variante.produit.nom} "
            f"| couleur={variante.couleur} | taille={variante.taille}"
        )
    return lines


def _resolve_variante(data: dict, analyzer):
    """Retrouve la variante choisie par le LLM (id puis code_jp), avec repli matching."""
    from ..models import Variante

    variante_id = data.get('variante_id')
    if isinstance(variante_id, (int, float)):
        variante = Variante.objects.select_related('produit').filter(id=int(variante_id)).first()
        if variante:
            return variante

    code_jp = data.get('code_jp')
    if code_jp:
        variante = (
            Variante.objects.select_related('produit')
            .filter(code_jp__iexact=str(code_jp).strip())
            .first()
        )
        if variante:
            return variante

    # Repli : matching par requete produit (code existant)
    product_query = (data.get('product_query') or '').strip()
    if product_query:
        match = analyzer.find_best_match(
            product_query,
            couleur=data.get('couleur') or None,
            taille=data.get('taille') or None,
        )
        if match:
            return match[1]
    return None


def analyze_comment(comment_text: str, *, vendeur_id=None, live_id=None) -> dict:
    """Analyse un commentaire de live (intention + produit choisi dans le catalogue)."""
    from ..ai import JPCommentAnalyzer  # import local : evite tout cycle

    analyzer = JPCommentAnalyzer()

    if client.is_enabled():
        try:
            catalog = _catalog_lines(vendeur_id=vendeur_id, live_id=live_id)
            data = client.generate_json(
                build_comment_prompt(comment_text, catalog),
                system=COMMENT_SYSTEM,
            )
            intent = 'achat' if str(data.get('intention', '')).lower() == 'achat' else 'inconnu'
            quantite = data.get('quantite')
            variante = _resolve_variante(data, analyzer)
            produit = variante.produit if variante else None

            return {
                'source': 'llm',
                'raw_text': comment_text,
                'intent': intent,
                'product_query': (data.get('product_query') or '').strip(),
                'couleur': data.get('couleur') or None,
                'taille': data.get('taille') or None,
                'quantite': int(quantite) if isinstance(quantite, (int, float)) else None,
                'produit_trouve': produit.nom if produit else None,
                'produit_id': produit.id if produit else None,
                'variante_id': variante.id if variante else None,
                'code_jp': variante.code_jp if variante else None,
                'catalog_size': len(catalog),
            }
        except client.LLMError as exc:
            logger.warning('Fallback regex (analyze_comment): %s', exc.message)

    result = analyzer.analyze(comment_text)
    result['source'] = 'regex'
    return result


def analyze_confirmation(message_text: str, client_context: dict | None = None) -> dict:
    """Analyse une reponse privee (extraction + decision suggeree)."""
    if client.is_enabled():
        try:
            today_iso = timezone.localdate().isoformat()
            data = client.generate_json(
                build_confirmation_prompt(
                    message_text,
                    client_context=client_context,
                    today_iso=today_iso,
                ),
                system=CONFIRMATION_SYSTEM,
            )
            intention = str(data.get('intention') or 'autre').lower()
            if intention not in {'annulation', 'confirmation', 'autre'}:
                intention = 'autre'
            fields = {
                'nom': data.get('nom') or None,
                'telephone': data.get('telephone') or None,
                'adresse': data.get('adresse') or None,
                'date_livraison': data.get('date_livraison') or None,
                'heure_livraison': data.get('heure_livraison') or None,
            }
            return {
                'source': 'llm',
                'raw_text': message_text,
                'intention': intention,
                'fields': fields,
                'decision_suggeree': _decision_from(intention, fields),
            }
        except client.LLMError as exc:
            logger.warning('Fallback regex (analyze_confirmation): %s', exc.message)

    from ..order_confirmation import _is_cancellation, parse_confirmation_text

    fields = parse_confirmation_text(message_text)
    if _is_cancellation(message_text):
        intention = 'annulation'
    elif fields:
        intention = 'confirmation'
    else:
        intention = 'autre'

    return {
        'source': 'regex',
        'raw_text': message_text,
        'intention': intention,
        'fields': fields,
        'decision_suggeree': _decision_from(intention, fields),
    }


def _decision_from(intention: str, fields: dict) -> str:
    if intention == 'annulation':
        return 'annuler_commande'
    missing = [field for field in REQUIRED_DELIVERY_FIELDS if not fields.get(field)]
    if missing:
        return 'demander_infos_manquantes: ' + ', '.join(missing)
    return 'confirmer_commande'
