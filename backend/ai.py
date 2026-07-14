import re

from django.db import models

from .llm_analyzer import LLMCommentAnalyzer, build_live_catalogue
from .models import Produit, Variante


class JPCommentAnalyzer:
    INTENT_PATTERNS = [
        r'JE\s*PRENDS',
        r'JP',
        r'JE\s*VOIS',
        r'VARIANTE',
        r'COMMAND(E|ER)',
    ]

    PRODUCT_SEARCH_PATTERNS = [
        r'JP\s+([A-Z0-9\s]+)',
        r'JE\s*PRENDS\s+([A-Z0-9\s]+)',
        r'VARIANTE\s+([A-Z0-9\s]+)',
        r'([A-Z0-9\s]+)\s+-\s*\d+\s*AR',
    ]

    QUANTITY_PATTERN = r'(?P<quantity>\d+)\s*(?:pcs|pi[eè]ces|x|EX|EX\s*)?'

    def analyze(self, comment_text: str) -> dict:
        cleaned = self.normalize(comment_text)
        intent = self.detect_intent(cleaned)
        product_query = self.extract_product_query(cleaned)
        # Couleur/taille déduites des VRAIES valeurs des variantes en base : ça
        # s'adapte à n'importe quel type de produit (pas seulement les vêtements)
        # et à n'importe quelle couleur/taille saisie par le vendeur.
        couleur = self.extract_attribute(cleaned, 'couleur')
        taille = self.extract_attribute(cleaned, 'taille')
        quantite = self.extract_first(self.QUANTITY_PATTERN, cleaned)
        match = self.find_best_match(product_query, couleur=couleur, taille=taille)
        produit = match[0] if match else None
        variante = match[1] if match else None

        return {
            'raw_text': comment_text,
            'cleaned_text': cleaned,
            'intent': intent,
            'product_query': product_query,
            'couleur': couleur,
            'taille': taille,
            'quantite': int(quantite) if quantite and quantite.isdigit() else None,
            'produit_trouve': produit.nom if produit else None,
            'produit_id': produit.id if produit else None,
            'variante_id': variante.id if variante else None,
            'code_jp': variante.code_jp if variante else None,
        }

    def normalize(self, text: str) -> str:
        text = text.upper()
        text = re.sub(r'[^A-Z0-9\s\-–]', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    def detect_intent(self, text: str) -> str:
        for pattern in self.INTENT_PATTERNS:
            if re.search(pattern, text):
                return 'achat'
        return 'inconnu'

    def extract_product_query(self, text: str) -> str:
        for pattern in self.PRODUCT_SEARCH_PATTERNS:
            match = re.search(pattern, text)
            if match:
                query = match.group(1)
                return self.clean_query(query)
        return text

    def extract_first(self, pattern: str, text: str) -> str | None:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
        return None

    def _known_attribute_values(self, field: str) -> list[str]:
        """Valeurs distinctes d'un attribut de variante (couleur, taille) en base.

        Mises en cache sur l'instance et triées par longueur décroissante pour
        privilégier la correspondance la plus spécifique (ex. « XXL » avant « L »).
        """
        cache = self.__dict__.setdefault('_attr_cache', {})
        if field not in cache:
            values = (
                Variante.objects.exclude(**{f'{field}__isnull': True})
                .exclude(**{field: ''})
                .values_list(field, flat=True)
                .distinct()
            )
            uniques = {value.strip() for value in values if value and value.strip()}
            cache[field] = sorted(uniques, key=len, reverse=True)
        return cache[field]

    def extract_attribute(self, cleaned_text: str, field: str) -> str | None:
        """Trouve dans le texte une couleur/taille existant réellement en base.

        Match par mot entier (insensible à la casse) sur le texte déjà normalisé,
        et renvoie la valeur telle qu'enregistrée (pour le filtrage en aval).
        """
        for value in self._known_attribute_values(field):
            if re.search(r'\b' + re.escape(value.upper()) + r'\b', cleaned_text):
                return value
        return None

    def clean_query(self, query: str) -> str:
        query = query.strip()
        query = re.sub(r'\s+', ' ', query)
        return query

    def find_best_match(self, query: str, couleur=None, taille=None):
        if not query:
            return None

        from .jp_codes import normalize_jp_code

        code = normalize_jp_code(query)
        if code:
            variante = (
                Variante.objects.filter(code_jp__iexact=code)
                .select_related('produit')
                .first()
            )
            if variante:
                return variante.produit, variante

        variante_qs = Variante.objects.select_related('produit').filter(
            models.Q(code_jp__icontains=query)
            | models.Q(produit__nom__icontains=query)
            | models.Q(couleur__icontains=query)
            | models.Q(taille__icontains=query)
        )
        if couleur:
            variante_qs = variante_qs.filter(couleur__icontains=couleur)
        if taille:
            variante_qs = variante_qs.filter(taille__icontains=taille)

        variante = variante_qs.first()
        if variante:
            return variante.produit, variante

        produit = Produit.objects.filter(nom__icontains=query).prefetch_related('variantes').first()
        if produit:
            first_variante = produit.variantes.order_by('id').first()
            return produit, first_variante

        tokens = [token for token in query.split() if len(token) > 1]
        for token in tokens:
            variante = Variante.objects.select_related('produit').filter(
                models.Q(code_jp__icontains=token)
                | models.Q(produit__nom__icontains=token)
                | models.Q(couleur__icontains=token)
                | models.Q(taille__icontains=token)
            ).first()
            if variante:
                return variante.produit, variante

        return None

    def find_best_produit(self, query: str):
        """Compatibilité ascendante pour les webhooks existants."""
        match = self.find_best_match(query)
        return match[0] if match else None


class HybridCommentAnalyzer:
    """Analyse hybride : regex d'abord (rapide, gratuit), LLM en repli (malgache).

    Même interface que JPCommentAnalyzer (analyze -> dict). La résolution finale
    produit/variante contre la base reste faite en aval (jp_capture), donc ni le
    regex ni le LLM ne peuvent imposer un produit inexistant.

    Stratégie :
    1. On lance le regex. S'il détecte une intention d'achat ET trouve un produit,
       c'est un cas net : on s'arrête là (gratuit, fiable).
    2. Sinon, si le LLM est activé, on lui demande de comprendre le message libre
       / malgache, en lui fournissant le catalogue réel du live pour l'ancrer.
    3. Sinon (LLM désactivé), on renvoie le résultat du regex tel quel.
    """

    REGEX_CONFIANCE = 0.9
    LOW_CONFIANCE_THRESHOLD = 0.5

    def __init__(self):
        self.regex = JPCommentAnalyzer()
        self.llm = LLMCommentAnalyzer()

    def analyze(self, comment_text: str, *, vendeur=None, live=None) -> dict:
        regex_result = self.regex.analyze(comment_text)
        regex_result['source'] = 'regex'
        regex_result['confiance'] = (
            self.REGEX_CONFIANCE if regex_result.get('produit_id') else None
        )

        # Cas net traité par le regex : intention d'achat + produit reconnu.
        if regex_result.get('intent') == 'achat' and regex_result.get('produit_id'):
            return regex_result

        # Repli LLM (uniquement si configuré) pour le langage libre / malgache.
        if self.llm.is_enabled():
            catalogue = build_live_catalogue(vendeur=vendeur, live=live)
            llm_result = self.llm.analyze(comment_text, catalogue=catalogue)
            if llm_result and llm_result.get('intent') == 'achat':
                confiance = llm_result.get('confiance')
                llm_result['needs_review'] = (
                    confiance is not None and confiance < self.LOW_CONFIANCE_THRESHOLD
                )
                return llm_result

        return regex_result


class ConfirmationMessageAnalyzer:

    PLACEHOLDER_NAMES = {'Client Live', 'Client Facebook', 'Client TikTok'}

    def analyze(self, text: str, client=None) -> dict:
        from .order_confirmation import (
            _is_quantity_line,
            _looks_like_date,
            _looks_like_phone,
            _looks_like_time,
            _normalize_phone,
            _prepare_confirmation_lines,
            parse_confirmation_text,
        )

        parsed = parse_confirmation_text(text)
        lines = _prepare_confirmation_lines(text)

        if client and not parsed and len(lines) == 1:
            parsed = self._infer_single_line(
                lines[0],
                client,
                _looks_like_phone,
                _looks_like_date,
                _looks_like_time,
                _normalize_phone,
                _is_quantity_line,
            )
        elif client:
            parsed = self._infer_from_missing_fields(
                lines,
                client,
                parsed,
                _looks_like_phone,
                _looks_like_date,
                _looks_like_time,
                _normalize_phone,
                _is_quantity_line,
            )

        if client and len(lines) == 1:
            line = lines[0]
            if (
                parsed.get('nom') == line
                and not self._needs_nom(client)
                and not getattr(client, 'adresse', None)
                and 'adresse' not in parsed
                and not _looks_like_phone(line)
                and not _looks_like_date(line)
                and not _looks_like_time(line)
                and not (_is_quantity_line and _is_quantity_line(line))
            ):
                parsed = {'adresse': parsed['nom']}

        if (
            client
            and parsed.get('nom')
            and parsed.get('telephone')
            and not parsed.get('adresse')
            and not self._needs_nom(client)
            and parsed.get('nom') != client.nom
        ):
            parsed = {**parsed, 'adresse': parsed['nom']}
            parsed.pop('nom', None)

        return {
            'raw_text': text,
            'fields': parsed,
        }

    def _needs_nom(self, client) -> bool:
        return not client.nom or client.nom in self.PLACEHOLDER_NAMES

    def _infer_single_line(self, line, client, looks_phone, looks_date, looks_time, normalize_phone, is_quantity_line=None):
        result = {}
        if is_quantity_line and is_quantity_line(line):
            # Nombre seul = quantité, traitée au niveau de la commande, pas un nom/adresse.
            return result
        if looks_phone(line):
            result['telephone'] = normalize_phone(line) or line
            return result
        if looks_time(line):
            result['heure_livraison'] = line
            return result
        if looks_date(line):
            result['date_livraison'] = line
            return result

        if self._needs_nom(client):
            result['nom'] = line
        elif not client.telephone and sum(ch.isdigit() for ch in line) >= 8:
            phone = normalize_phone(line)
            if phone:
                result['telephone'] = phone
        elif not client.adresse:
            result['adresse'] = line
        elif not client.date_livraison_preferee and looks_date(line):
            result['date_livraison'] = line
        elif not client.heure_livraison_preferee and looks_time(line):
            result['heure_livraison'] = line
        return result

    def _infer_from_missing_fields(self, lines, client, parsed, looks_phone, looks_date, looks_time, normalize_phone, is_quantity_line=None):
        for line in lines:
            if line in parsed.values():
                continue
            if is_quantity_line and is_quantity_line(line):
                continue
            is_text = not looks_phone(line) and not looks_date(line) and not looks_time(line)
            if 'telephone' not in parsed and not client.telephone and looks_phone(line):
                parsed['telephone'] = normalize_phone(line) or line
            elif 'heure_livraison' not in parsed and not client.heure_livraison_preferee and looks_time(line):
                parsed['heure_livraison'] = line
            elif 'date_livraison' not in parsed and not client.date_livraison_preferee and looks_date(line):
                parsed['date_livraison'] = line
            elif (
                'nom' not in parsed
                and self._needs_nom(client)
                and is_text
            ):
                # Le nom manque encore : la première ligne texte libre le complète,
                # peu importe l'ordre d'arrivée des autres informations.
                parsed['nom'] = line
            elif (
                'adresse' not in parsed
                and not client.adresse
                and is_text
                and line != parsed.get('nom')
                and line != (client.nom if not self._needs_nom(client) else None)
            ):
                parsed.setdefault('adresse', line)
        return parsed
