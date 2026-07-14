"""Compréhension des commentaires par LLM gratuit (repli de l'analyse regex).

Pourquoi : le regex (JPCommentAnalyzer) gère bien les formats prévisibles
(« JP NOIR L ») mais « casse » sur le langage libre, les fautes et surtout le
malgache (« mividy aho », « alaiko ity mena ity », « mbola misy ve »…).

Ce module appelle un LLM via une API compatible OpenAI (chat completions). Ça
marche tel quel avec une offre GRATUITE comme Groq (free tier) ou un Ollama
local — il suffit de pointer AZLIVE_LLM_BASE_URL / _API_KEY / _MODEL.

Principes :
- Désactivé par défaut : sans clé configurée, analyze() renvoie None et le
  pipeline retombe proprement sur le regex (les tests restent hermétiques).
- Le LLM ne fait QUE de l'extraction structurée (intention + champs). Il ne
  rédige aucun message client et n'invente aucun prix/stock.
- La sortie est du JSON strict, mappé EXACTEMENT sur la forme renvoyée par
  JPCommentAnalyzer.analyze (mêmes clés), plus 'confiance' et 'source'.
- La résolution produit/variante réelle reste faite en aval contre la base
  (jp_capture), donc le LLM ne peut pas halluciner un produit inexistant.
"""

import json
import logging
import urllib.error
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)


# Glossaire live malgache injecté dans le prompt : aide le modèle sur l'argot et
# le code-switching (mélange malgache/français) typiques des lives à Madagascar.
MALAGASY_LEXICON = (
    "mividy / maka / alaiko / haka = acheter (intention d'achat) ; "
    "JP / je prends = acheter ; "
    "ohatrinona / hoatrinona / ohatrinona moa = quelle est le prix ; "
    "mbola misy ve / misy ve = est-ce encore disponible (question stock) ; "
    "afaka / misy = disponible ; "
    "tsy misy / lany = en rupture ; "
    "foano / esory / tsy maka intsony = annuler ; "
    "mena=rouge, manga=bleu, mainty=noir, fotsy=blanc, maintso=vert, "
    "mavo=jaune, volomparasy=violet, volonkena=marron."
)

SYSTEM_PROMPT = (
    "Tu es un assistant d'un commerce en live à Madagascar. Les clients commentent "
    "en malgache, en français, ou un mélange des deux, souvent avec des fautes. "
    "Analyse UN commentaire et renvoie UNIQUEMENT un objet JSON valide (aucun texte "
    "autour), avec ces clés :\n"
    '- "intent": une parmi "achat", "question_prix", "question_stock", "lieu", '
    '"annulation", "salutation", "autre".\n'
    '- "product_query": le nom/description du produit visé (string, "" si absent).\n'
    '- "code_jp": le code de variante cité s\'il y en a un (string ou null).\n'
    '- "couleur": couleur citée, en français (string ou null).\n'
    '- "taille": taille citée, ex. S, M, L, XL (string ou null).\n'
    '- "quantite": quantité demandée (entier ou null).\n'
    '- "confiance": ta confiance globale entre 0 et 1 (nombre).\n'
    'NOTE: utilise "lieu" quand le client demande où se trouve le magasin, '
    'comment se passe la livraison, ou où livrer (aiza / livraison / antoandro / adresse).\n'
    "Glossaire utile : " + MALAGASY_LEXICON
    + " ; aiza / taiza = où (lieu) ; misy toerana ve = est-ce qu'il y a un endroit ;"
    " livraison / aterina = livraison."
)


class LLMCommentAnalyzer:
    """Analyse un commentaire via LLM. Même interface que JPCommentAnalyzer."""

    def __init__(self):
        self.base_url = getattr(settings, 'AZLIVE_LLM_BASE_URL', '').rstrip('/')
        self.api_key = getattr(settings, 'AZLIVE_LLM_API_KEY', '')
        self.model = getattr(settings, 'AZLIVE_LLM_MODEL', '')
        self.timeout = getattr(settings, 'AZLIVE_LLM_TIMEOUT', 12)
        self.enabled = bool(
            getattr(settings, 'AZLIVE_LLM_ENABLED', False)
            and self.base_url
            and self.model
        )

    def is_enabled(self) -> bool:
        return self.enabled

    def analyze(self, comment_text: str, *, catalogue: str | None = None) -> dict | None:
        """Renvoie une analyse structurée, ou None si désactivé / erreur.

        catalogue : texte optionnel listant les codes/produits réellement
        disponibles dans le live, pour ancrer le modèle sur l'offre réelle.
        """
        if not self.enabled or not comment_text:
            return None

        try:
            raw = self._call_llm(comment_text, catalogue=catalogue)
        except (urllib.error.URLError, TimeoutError) as exc:
            logger.warning('LLM indisponible: %s', exc)
            return None
        except Exception as exc:  # noqa: BLE001 — un repli robuste ne doit jamais casser le flux
            logger.warning('Erreur LLM inattendue: %s', exc)
            return None

        fields = self._parse_json(raw)
        if fields is None:
            return None

        return self._to_analysis(comment_text, fields)

    def _call_llm(self, comment_text: str, *, catalogue: str | None = None) -> str:
        user_content = comment_text.strip()
        if catalogue:
            user_content = (
                f"Produits/codes disponibles dans ce live :\n{catalogue}\n\n"
                f"Commentaire du client :\n{comment_text.strip()}"
            )

        payload = {
            'model': self.model,
            'messages': [
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': user_content},
            ],
            'temperature': 0,
            'response_format': {'type': 'json_object'},
        }
        data = json.dumps(payload).encode('utf-8')
        request = urllib.request.Request(
            f'{self.base_url}/chat/completions',
            data=data,
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {self.api_key}',
                'User-Agent': 'AZLive/1.0',
            },
            method='POST',
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = json.loads(response.read().decode('utf-8'))
        return body['choices'][0]['message']['content']

    @staticmethod
    def _parse_json(raw: str) -> dict | None:
        if not raw:
            return None
        text = raw.strip()
        # Certains modèles entourent le JSON de ```json ... ``` : on nettoie.
        if text.startswith('```'):
            text = text.strip('`')
            if text.lower().startswith('json'):
                text = text[4:]
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            start, end = text.find('{'), text.rfind('}')
            if start == -1 or end == -1 or end <= start:
                return None
            try:
                parsed = json.loads(text[start : end + 1])
            except (json.JSONDecodeError, ValueError):
                return None
        return parsed if isinstance(parsed, dict) else None

    def _to_analysis(self, comment_text: str, fields: dict) -> dict:
        """Mappe la sortie LLM sur la forme JPCommentAnalyzer.analyze + extras.

        La résolution produit/variante contre la base est faite en aval, mais on
        tente quand même un find_best_match pour renseigner produit_id/variante_id
        comme le fait l'analyseur regex (parité de forme).
        """
        from .ai import JPCommentAnalyzer

        intent = str(fields.get('intent') or 'autre').lower()
        product_query = (fields.get('product_query') or '').strip()
        couleur = self._clean(fields.get('couleur'))
        taille = self._clean(fields.get('taille'))
        code_jp = self._clean(fields.get('code_jp'))
        quantite = fields.get('quantite')
        confiance = self._clean_float(fields.get('confiance'))

        regex = JPCommentAnalyzer()
        query = product_query or code_jp or comment_text
        match = regex.find_best_match(query, couleur=couleur, taille=taille)
        produit = match[0] if match else None
        variante = match[1] if match else None

        return {
            'raw_text': comment_text,
            'cleaned_text': regex.normalize(comment_text),
            'intent': 'achat' if intent == 'achat' else intent,
            'product_query': product_query,
            'couleur': couleur,
            'taille': taille,
            'quantite': int(quantite) if isinstance(quantite, (int, float)) and quantite else None,
            'produit_trouve': produit.nom if produit else None,
            'produit_id': produit.id if produit else None,
            'variante_id': variante.id if variante else None,
            'code_jp': (variante.code_jp if variante else code_jp) or None,
            'confiance': confiance,
            'source': 'llm',
        }

    @staticmethod
    def _clean(value):
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _clean_float(value) -> float | None:
        try:
            score = float(value)
        except (TypeError, ValueError):
            return None
        return max(0.0, min(1.0, score))


def build_live_catalogue(vendeur=None, live=None, limit: int = 60) -> str:
    """Construit un petit catalogue texte (codes/produits dispo) pour le prompt.

    Scopé au live si fourni (codes JP attribués), sinon au vendeur. Sert à ancrer
    le LLM sur l'offre réelle plutôt que de le laisser deviner.
    """
    from .models import LiveCodeJP, Variante

    lignes = []
    if live is not None:
        mappings = (
            LiveCodeJP.objects.filter(live=live)
            .select_related('variante', 'variante__produit')[:limit]
        )
        for mapping in mappings:
            var = mapping.variante
            produit = var.produit if var else None
            details = ', '.join(
                part for part in [getattr(var, 'couleur', ''), getattr(var, 'taille', '')] if part
            )
            nom = produit.nom if produit else '?'
            lignes.append(f'- {mapping.code} = {nom}{(" (" + details + ")") if details else ""}')

    if not lignes:
        variantes = Variante.objects.select_related('produit')
        if vendeur is not None:
            variantes = variantes.filter(produit__vendeur=vendeur)
        for var in variantes[:limit]:
            details = ', '.join(part for part in [var.couleur, var.taille] if part)
            code = f'{var.code_jp} = ' if var.code_jp else ''
            lignes.append(f'- {code}{var.produit.nom}{(" (" + details + ")") if details else ""}')

    return '\n'.join(lignes)
