"""Helpers de normalisation et d'affichage des codes JP.

Le code JP stocke est NU (sans le prefixe "JP"). Le mot "JP" reste seulement :
  - le mot-cle d'intention tape par le client dans un commentaire ("JP NOIR"),
  - un prefixe d'affichage ("JP NOIR") sur les etiquettes / factures.

Cela evite le double prefixe "JP JP" quand un client repond "JP <code>" alors que
le code lui-meme commencait deja par "JP".
"""

import re

_JP_PREFIX_RE = re.compile(r'^(?:\s*JP[\s\-_]*)+', re.IGNORECASE)


def normalize_jp_code(raw) -> str:
    """Retourne un code JP nu, normalise (sans prefixe 'JP', trim, majuscules).

    Exemples :
      "JP NOIR" -> "NOIR"
      "JPNOIR"  -> "NOIR"
      " jp-001" -> "001"
      "NOIR"    -> "NOIR"
      None      -> ""
    """
    if raw is None:
        return ''
    code = str(raw).strip()
    # On retire un eventuel prefixe "JP" en tete (avec separateurs optionnels).
    stripped = _JP_PREFIX_RE.sub('', code)
    # Securite : si le code n'etait QUE "JP", on garde la valeur d'origine nettoyee.
    code = stripped if stripped else code
    return re.sub(r'\s+', ' ', code).strip().upper()


def format_jp_code(code) -> str:
    """Affichage normalise d'un code JP : 'JP {code}'.

    Le code est d'abord normalise pour ne jamais produire 'JP JP...'.
    """
    bare = normalize_jp_code(code)
    return f'JP {bare}'.strip() if bare else 'JP'


def resolve_live_code(live, variante) -> str:
    """Code nu attribue a la variante dans ce live, sinon code catalogue de la variante."""
    if variante is None:
        return ''
    if live is not None:
        from .models import LiveCodeJP

        mapping = LiveCodeJP.objects.filter(live=live, variante=variante).first()
        if mapping and mapping.code:
            return normalize_jp_code(mapping.code)
    return normalize_jp_code(getattr(variante, 'code_jp', ''))


def code_for_commande(commande) -> str:
    """Code JP nu pertinent pour une commande (code du live, sinon code catalogue)."""
    variante = commande.variante if commande.variante_id else None
    if variante is None and commande.produit_id:
        variante = commande.produit.variantes.order_by('id').first()
    live = commande.live if commande.live_id else None
    return resolve_live_code(live, variante)
