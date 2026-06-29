from django.core.exceptions import ValidationError


def validate_variante_uniqueness(produit, taille, couleur, exclude_pk=None):
    """Une taille + couleur ne peut pas être dupliquée pour un même produit."""
    from .models import Variante

    qs = Variante.objects.filter(
        produit=produit,
        taille__iexact=taille.strip(),
        couleur__iexact=couleur.strip(),
    )
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    if qs.exists():
        raise ValidationError(
            f'La combinaison taille "{taille}" et couleur "{couleur}" existe déjà pour ce produit.'
        )


def validate_code_jp_uniqueness(code_jp, produit=None, exclude_pk=None):
    """Valide le code JP « par défaut » d'une variante (code catalogue, en repli).

    Le code n'est plus unique globalement : l'unicité réelle est portée par live via
    le modèle LiveCodeJP (unique par live). Ici on se contente de :
      - normaliser (retrait d'un éventuel préfixe « JP », trim, majuscules) ;
      - autoriser un code vide/nul (les codes peuvent être attribués par live) ;
      - éviter qu'un même produit ait deux variantes avec le même code de repli
        (sinon la résolution hors live serait ambiguë).
    """
    from .jp_codes import normalize_jp_code
    from .models import Variante

    normalized = normalize_jp_code(code_jp)
    if not normalized:
        return

    if produit is None:
        return

    qs = Variante.objects.filter(produit=produit, code_jp__iexact=normalized)
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    if qs.exists():
        raise ValidationError(
            f'Le code JP "{normalized}" est déjà utilisé par une autre variante de ce produit.'
        )
