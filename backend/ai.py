import re

from django.db import models

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
    COLOR_PATTERN = r'(ROUGE|BLEU|NOIR|BLANC|VERT|JAUNE|ROSE|MARRON|OR|ARGENT)'
    SIZE_PATTERN = r'(S|M|L|XL|XXL|XS|XXS)'

    def analyze(self, comment_text: str) -> dict:
        cleaned = self.normalize(comment_text)
        intent = self.detect_intent(cleaned)
        product_query = self.extract_product_query(cleaned)
        couleur = self.extract_first(self.COLOR_PATTERN, cleaned)
        taille = self.extract_first(self.SIZE_PATTERN, cleaned)
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


class ConfirmationMessageAnalyzer:
    """Analyse libre des réponses client — sans format imposé, avec complétion progressive."""

    PLACEHOLDER_NAMES = {'Client Live', 'Client Facebook', 'Client TikTok'}

    def analyze(self, text: str, client=None) -> dict:
        from .order_confirmation import (
            _is_quantity_line,
            _looks_like_date,
            _looks_like_phone,
            _looks_like_time,
            _normalize_phone,
            parse_confirmation_text,
        )

        parsed = parse_confirmation_text(text)
        lines = [line.strip() for line in (text or '').splitlines() if line.strip()]

        if client and len(lines) == 1:
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
