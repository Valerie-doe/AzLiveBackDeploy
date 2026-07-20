"""Formulaire public de collecte d'informations client (live TikTok).

TikTok n'autorise pas l'automatisation des messages privés (DM). Le vendeur partage
un lien public par live. Le client s'identifie automatiquement via TikTok Login :
l'app récupère son @ et retrouve ses commandes JP capturées pendant le live, ou lui
indique de commander d'abord dans les commentaires du live.

Endpoints (AllowAny) :
  GET  /api/public/lives/<live_id>/tiktok-login/     → URL OAuth TikTok (client)
  GET  /api/public/tiktok/callback/                   → callback OAuth → redirect frontend
  GET  /api/public/lives/<live_id>/order-form/?handle=<@tiktok>
  POST /api/public/lives/<live_id>/order-form/
"""
import urllib.parse

from django.conf import settings
from django.db import models
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .jp_capture import normalize_tiktok_username
from .models import Client, Commande, Live, LiveCodeJP
from .order_confirmation import (
    OrderConfirmationError,
    cancel_commande_public,
    confirm_commande_from_message,
    _missing_confirmation_fields,
    _stock_remaining_for,
)
from .tiktok_oauth import (
    TikTokOAuthError,
    authenticate_public_client_with_code,
    build_public_oauth_url,
    generate_public_oauth_state,
    tiktok_configured,
)

REQUIRED_CLIENT_FIELDS = ('nom', 'telephone', 'adresse', 'date_livraison', 'heure_livraison')


def _match_clients(handle: str):
    """Clients correspondant à un @TikTok (insensible à la casse, @ et espaces ignorés)."""
    normalized = normalize_tiktok_username(handle)
    if not normalized:
        return Client.objects.none()
    return Client.objects.filter(
        models.Q(tiktok_id__iexact=normalized) | models.Q(social_handle__iexact=normalized)
    )


def _live_code_map(live: Live, variante_ids) -> dict[int, str]:
    """Code JP propre au live pour chaque variante (repli sur le code catalogue)."""
    mapping = {}
    for entry in LiveCodeJP.objects.filter(live=live, variante_id__in=variante_ids):
        mapping[entry.variante_id] = entry.code
    return mapping


def _pending_commandes(live: Live, clients):
    return (
        Commande.objects.select_related('produit', 'variante', 'client')
        .filter(live=live, client__in=clients, statut=Commande.STATUT_JP_CAPTURE)
        .order_by('ordre_jp')
    )


def _commande_is_eligible(commande: Commande) -> bool:
    """Tête de file + assez de stock pour confirmer maintenant."""
    from .order_confirmation import _order_is_eligible

    return _order_is_eligible(commande)

def _infos_completes(commande: Commande) -> bool:
    return not _missing_confirmation_fields(commande)


def _is_waitlisted_complete(commande: Commande) -> bool:
    """Infos déjà remplies + pas encore éligible → vraie liste d'attente (ne pas re-afficher le formulaire)."""
    return _infos_completes(commande) and not _commande_is_eligible(commande)


def _serialize_commandes(live: Live, commandes) -> list[dict]:
    variante_ids = [c.variante_id for c in commandes if c.variante_id]
    code_map = _live_code_map(live, variante_ids)
    items = []
    for commande in commandes:
        variante = commande.variante
        code = code_map.get(commande.variante_id) or (variante.code_jp if variante else '')
        remaining = _stock_remaining_for(commande)
        stock_actuel = variante.stock if variante else None
        eligible = _commande_is_eligible(commande)
        infos_ok = _infos_completes(commande)
        items.append(
            {
                'commande_id': commande.id,
                'produit': commande.produit.nom,
                'code_jp': code,
                'taille': variante.taille if variante else '',
                'couleur': variante.couleur if variante else '',
                'prix_unitaire': str(variante.prix_unitaire) if variante else None,
                'quantite': commande.quantite,
                'stock_disponible': remaining,
                'stock_actuel': stock_actuel,
                'en_rupture': remaining is not None and remaining <= 0,
                'en_liste_attente': not eligible,
                'infos_completes': infos_ok,
                'pret_a_confirmer': eligible and infos_ok,
            }
        )
    return items


def _split_pending_commandes(live: Live, clients):
    """Sépare les JP confirmables / à compléter des JP déjà en liste d'attente."""
    pending = list(_pending_commandes(live, clients))
    a_traiter = []
    en_attente = []
    for commande in pending:
        if _is_waitlisted_complete(commande):
            en_attente.append(commande)
        else:
            a_traiter.append(commande)
    return a_traiter, en_attente


class PublicOrderFormAPIView(APIView):
    """Recherche (GET) et complétion (POST) des commandes d'un client pour un live."""

    permission_classes = [AllowAny]

    def get(self, request, live_id: int):
        live = get_object_or_404(Live, pk=live_id)
        handle = request.query_params.get('handle', '')

        base = {
            'live': {'id': live.id, 'titre': live.titre, 'statut': live.statut},
            'vendeur': live.vendeur.nom if live.vendeur_id else '',
        }

        if not handle.strip():
            return Response(
                {
                    **base,
                    'found': False,
                    'commandes': [],
                    'commandes_liste_attente': [],
                },
                status=status.HTTP_200_OK,
            )

        clients = _match_clients(handle)
        if not clients.exists():
            return Response(
                {
                    **base,
                    'found': False,
                    'commandes': [],
                    'commandes_liste_attente': [],
                },
                status=status.HTTP_200_OK,
            )

        commandes_a_traiter, commandes_attente = _split_pending_commandes(live, clients)
        client = clients.first()
        return Response(
            {
                **base,
                'found': True,
                'client': {
                    'nom': client.nom,
                    'telephone': client.telephone,
                    'adresse': client.adresse,
                    'date_livraison': (
                        client.date_livraison_preferee.isoformat()
                        if client.date_livraison_preferee
                        else ''
                    ),
                    'heure_livraison': (
                        client.heure_livraison_preferee.strftime('%H:%M')
                        if client.heure_livraison_preferee
                        else ''
                    ),
                },
                # Uniquement les JP à compléter / à confirmer (stock disponible).
                'commandes': _serialize_commandes(live, commandes_a_traiter),
                # Déjà en file d'attente (infos OK, stock pas encore libre) — ne pas re-afficher le formulaire.
                'commandes_liste_attente': _serialize_commandes(live, commandes_attente),
            },
            status=status.HTTP_200_OK,
        )

    def post(self, request, live_id: int):
        live = get_object_or_404(Live, pk=live_id)
        data = request.data or {}

        handle = (data.get('handle') or '').strip()
        clients = _match_clients(handle)
        if not handle or not clients.exists():
            return Response(
                {'detail': 'Aucune commande trouvée pour ce compte TikTok. Vérifiez votre identifiant @.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Validation des champs client obligatoires.
        missing = [f for f in REQUIRED_CLIENT_FIELDS if not str(data.get(f, '')).strip()]
        if missing:
            return Response(
                {'detail': 'Champs obligatoires manquants.', 'champs_manquants': missing},
                status=status.HTTP_400_BAD_REQUEST,
            )

        items = data.get('items') or []
        if not isinstance(items, list) or not items:
            return Response(
                {'detail': 'Veuillez sélectionner au moins une commande à valider.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Commandes éligibles du client pour ce live (anti-falsification d'ID).
        # On exclut les JP déjà en liste d'attente (infos OK + pas encore de place).
        allowed = {
            c.id: c
            for c in _pending_commandes(live, clients)
            if not _is_waitlisted_complete(c)
        }

        parsed_data = {
            'nom': str(data.get('nom')).strip(),
            'telephone': str(data.get('telephone')).strip(),
            'adresse': str(data.get('adresse')).strip(),
            'date_livraison': str(data.get('date_livraison')).strip(),
            'heure_livraison': str(data.get('heure_livraison')).strip(),
        }

        results = []
        errors = []
        for item in items:
            try:
                commande_id = int(item.get('commande_id'))
                quantite = int(item.get('quantite'))
            except (TypeError, ValueError):
                errors.append({'item': item, 'detail': 'commande_id et quantite doivent être des entiers.'})
                continue

            commande = allowed.get(commande_id)
            if commande is None:
                errors.append({'commande_id': commande_id, 'detail': 'Commande introuvable pour ce compte/live.'})
                continue
            if quantite <= 0:
                errors.append({'commande_id': commande_id, 'detail': 'La quantité doit être supérieure à 0.'})
                continue

            commande.quantite = quantite
            commande.save(update_fields=['quantite'])

            # Recharge la variante pour voir le stock à jour (après une confirmation précédente
            # dans la même requête multi-commandes).
            if commande.variante_id:
                commande.variante.refresh_from_db()

            try:
                outcome = confirm_commande_from_message(
                    commande,
                    parsed_data,
                    inbound_text='Informations transmises via le formulaire de commande (TikTok).',
                    canal='TikTok',
                )
                # Liste d'attente = infos OK, pas encore de stock → succès partiel, pas une erreur.
                results.append(
                    {
                        'commande_id': commande_id,
                        'status': outcome.get('status'),
                        'complet': bool(outcome.get('complet')),
                        'en_attente': bool(outcome.get('en_attente')),
                    }
                )
            except OrderConfirmationError as exc:
                detail = exc.message
                payload = getattr(exc, 'payload', None) or {}
                errors.append(
                    {
                        'commande_id': commande_id,
                        'detail': detail,
                        'rupture_stock': bool(payload.get('rupture_stock')),
                    }
                )
            except ValueError as exc:
                errors.append(
                    {
                        'commande_id': commande_id,
                        'detail': str(exc),
                        'rupture_stock': True,
                    }
                )

        return Response(
            {
                'status': 'Informations enregistrées.',
                'traitees': results,
                'erreurs': errors,
            },
            status=status.HTTP_200_OK if results else status.HTTP_400_BAD_REQUEST,
        )


class PublicOrderCancelAPIView(APIView):
    """Annule une ou plusieurs commandes JP depuis le formulaire public."""

    permission_classes = [AllowAny]

    def post(self, request, live_id: int):
        live = get_object_or_404(Live, pk=live_id)
        data = request.data or {}
        handle = (data.get('handle') or '').strip()
        clients = _match_clients(handle)
        if not handle or not clients.exists():
            return Response(
                {'detail': 'Aucune commande trouvée pour ce compte TikTok.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        raw_ids = data.get('commande_ids') or data.get('items') or []
        if not isinstance(raw_ids, list) or not raw_ids:
            # Sans liste : annule toutes les commandes JP en attente du client pour ce live.
            pending = list(_pending_commandes(live, clients))
            commande_ids = [c.id for c in pending]
        else:
            commande_ids = []
            for item in raw_ids:
                try:
                    if isinstance(item, dict):
                        commande_ids.append(int(item.get('commande_id')))
                    else:
                        commande_ids.append(int(item))
                except (TypeError, ValueError):
                    continue

        if not commande_ids:
            return Response(
                {'detail': 'Aucune commande à annuler.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Anti-falsification : uniquement les commandes du client sur ce live,
        # encore annulables (jp_capture / confirme / prepare).
        allowed = {
            c.id: c
            for c in Commande.objects.select_related('produit', 'variante', 'client').filter(
                live=live,
                client__in=clients,
                id__in=commande_ids,
                statut__in=(
                    Commande.STATUT_JP_CAPTURE,
                    Commande.STATUT_CONFIRME,
                    Commande.STATUT_PREPARE,
                ),
            )
        }

        annulees = []
        errors = []
        for commande_id in commande_ids:
            commande = allowed.get(commande_id)
            if commande is None:
                errors.append(
                    {
                        'commande_id': commande_id,
                        'detail': 'Commande introuvable ou non annulable pour ce compte/live.',
                    }
                )
                continue
            try:
                outcome = cancel_commande_public(commande)
                annulees.append(
                    {
                        'commande_id': commande_id,
                        'status': outcome.get('status'),
                        'annule': True,
                    }
                )
            except OrderConfirmationError as exc:
                errors.append({'commande_id': commande_id, 'detail': exc.message})

        return Response(
            {
                'status': 'Annulation traitée.',
                'annulees': annulees,
                'erreurs': errors,
            },
            status=status.HTTP_200_OK if annulees else status.HTTP_400_BAD_REQUEST,
        )


def _public_order_frontend_url(live_id: int, **query) -> str:
    base = settings.AZLIVE_PUBLIC_ORDER_BASE_URL.rstrip('/')
    qs = urllib.parse.urlencode(query)
    return f'{base}/commander/{live_id}' + (f'?{qs}' if qs else '')


class PublicTikTokLoginAPIView(APIView):
    """Démarre l'identification TikTok pour un client (formulaire public)."""

    permission_classes = [AllowAny]

    def get(self, request, live_id: int):
        get_object_or_404(Live, pk=live_id)
        if not tiktok_configured():
            return Response(
                {'detail': 'Connexion TikTok non configurée sur le serveur.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        state, challenge = generate_public_oauth_state(live_id)
        return Response(
            {'auth_url': build_public_oauth_url(state, challenge)},
            status=status.HTTP_200_OK,
        )


class PublicTikTokCallbackAPIView(APIView):
    """Callback OAuth TikTok client → redirection vers le formulaire avec handle."""

    permission_classes = [AllowAny]

    def get(self, request):
        error = request.query_params.get('error')
        live_id_param = request.query_params.get('live_id', '1')

        if error:
            description = request.query_params.get('error_description', error)
            try:
                live_id = int(live_id_param)
            except ValueError:
                live_id = 1
            return HttpResponseRedirect(
                _public_order_frontend_url(live_id, error=description)
            )

        code = request.query_params.get('code')
        state = request.query_params.get('state')
        if not code:
            return HttpResponseRedirect(
                _public_order_frontend_url(1, error='Connexion TikTok annulée.')
            )

        try:
            live_id, handle = authenticate_public_client_with_code(code, state)
            return HttpResponseRedirect(
                _public_order_frontend_url(live_id, handle=handle)
            )
        except TikTokOAuthError as exc:
            return HttpResponseRedirect(
                _public_order_frontend_url(1, error=exc.message)
            )
