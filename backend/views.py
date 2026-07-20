import re
from datetime import timedelta

from django.db import models, transaction
from django.db.models import Max, Sum, Count, Value
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.permissions import IsAuthenticated, AllowAny

from .ai import JPCommentAnalyzer
from .message_humanizer import emoji, greeting, pick
from .models import Client, Commande, Livraison, Livreur, Paiement, Produit, ProduitImage, Vendeur, Message, Collaborateur, Live, LiveCodeJP, Variante, PageFacebook, ParametresPlateforme
from .serializers import (
    ClientSerializer,
    CommandeSerializer,
    LivraisonSerializer,
    LivreurSerializer,
    PaiementSerializer,
    ProduitImageSerializer,
    ProduitSerializer,
    VendeurSerializer,
    MessageSerializer,
    CollaborateurSerializer,
    LiveSerializer,
    LiveCodeJPSerializer,
    VarianteSerializer,
    PageFacebookSerializer,
    ParametresPlateformeSerializer,
)
from .services import MessagingService, AZExpressService
from .facebook_oauth import FacebookOAuthError, facebook_configured, sync_vendeur_pages
from .jp_codes import code_for_commande, format_jp_code, normalize_jp_code
from .jp_capture import create_jp_commande
from .live_service import arreter_live, demarrer_live
from .documents import build_etiquette_livraison_pdf, build_facture_pdf, pdf_response
from .order_confirmation import (
    OrderConfirmationError,
    analyze_confirmation_message,
    handle_client_reply,
    process_inbound_private_message,
)


def _commande_variante(commande):
    if commande.variante_id:
        return commande.variante
    return commande.produit.variantes.order_by('id').first()


def _commande_variante_payload(commande):
    variante = _commande_variante(commande)
    if not variante:
        return {
            'taille': '',
            'couleur': '',
            'prix': '0',
            'code_jp': '',
        }
    return {
        'taille': variante.taille,
        'couleur': variante.couleur,
        'prix': str(variante.prix_unitaire),
        'code_jp': code_for_commande(commande),
    }


class VendeurListCreateView(generics.ListCreateAPIView):
    queryset = Vendeur.objects.all()
    serializer_class = VendeurSerializer


class ProduitListCreateView(generics.ListCreateAPIView):
    queryset = Produit.objects.select_related('vendeur').prefetch_related('variantes', 'images').all().order_by('id')
    serializer_class = ProduitSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]


class ProduitDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Produit.objects.select_related('vendeur').prefetch_related('variantes', 'images').all()
    serializer_class = ProduitSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]


class ProduitImageDeleteView(generics.DestroyAPIView):
    queryset = ProduitImage.objects.select_related('produit').all()
    serializer_class = ProduitImageSerializer

    def perform_destroy(self, instance):
        produit = instance.produit
        instance.delete()
        first = produit.images.order_by('created_at', 'id').first()
        produit.photo = first.image if first else None
        produit.save(update_fields=['photo'])


class CommandeListCreateView(generics.ListCreateAPIView):
    serializer_class = CommandeSerializer

    def get_queryset(self):
        # select_related/prefetch_related : évite les requêtes N+1 lors de la sérialisation
        # imbriquée (client, produit + variantes/images, live + dressing), notamment pour la
        # vue Clients qui agrège toutes les commandes d'un vendeur (?vendeur_id=).
        queryset = (
            Commande.objects
            .select_related('client', 'produit', 'variante', 'paiement', 'livraison', 'live')
            .prefetch_related(
                'produit__variantes',
                'produit__images',
                'live__produits_dressing__variantes',
                'live__produits_dressing__images',
            )
            .all()
        )
        live_id = self.request.query_params.get('live_id')
        client_id = self.request.query_params.get('client_id')
        produit_id = self.request.query_params.get('produit_id')
        vendeur_id = self.request.query_params.get('vendeur_id')

        if live_id:
            queryset = queryset.filter(live_id=live_id)
            # Le frontend interroge cette route toutes les 5 s pendant un live : on en profite
            # pour relancer le poller de commentaires Facebook s'il est mort (reload Django).
            live = (
                Live.objects.filter(pk=live_id, statut=Live.STATUT_EN_COURS)
                .select_related('vendeur')
                .first()
            )
            if live and not live.vendeur.is_demo_mode:
                from .facebook_live_comments import ensure_facebook_comment_listener

                ensure_facebook_comment_listener(live)
        if client_id:
            queryset = queryset.filter(client_id=client_id)
        if produit_id:
            queryset = queryset.filter(produit_id=produit_id)
        if vendeur_id:
            queryset = queryset.filter(produit__vendeur_id=vendeur_id)

        return queryset


class CommandeDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Commande.objects.select_related('client', 'produit', 'variante').all()
    serializer_class = CommandeSerializer


class JPCaptureAPIView(APIView):
    permission_classes = [AllowAny]  # MVP — accessible sans token

    def post(self, request):
        comment_text = request.data.get('comment_text', '')
        if not comment_text:
            return Response({'detail': 'Le champ comment_text est requis.'}, status=status.HTTP_400_BAD_REQUEST)

        parsed = JPCommentAnalyzer().analyze(comment_text)
        product_query = parsed.get('product_query') or self.extract_product_query(comment_text)
        match = self.find_best_match(product_query, parsed.get('couleur'), parsed.get('taille'))
        if match is None:
            return Response(
                {
                    'detail': "Produit introuvable pour ce JP.",
                    'product_query': product_query,
                    'ai_analysis': parsed,
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        produit, variante = match

        client, _created = Client.objects.get_or_create(
            telephone=request.data.get('telephone', ''),
            defaults={
                'nom': request.data.get('nom', 'Client Live'),
                'adresse': request.data.get('adresse', ''),
                'date_livraison_preferee': request.data.get('date_livraison_preferee', None),
            },
        )

        max_order = Commande.objects.filter(produit=produit).aggregate(max_ordre=Max('ordre_jp'))['max_ordre'] or 0
        ordre_jp = max_order + 1
        commande = Commande.objects.create(
            client=client,
            produit=produit,
            variante=variante,
            ordre_jp=ordre_jp,
        )

        if ordre_jp == 1:
            message_content = self.build_auto_message(client, produit)
        else:
            intro = pick([
                f"{greeting(client.nom)} 😊 Voaray ny JP-nao ho an'ny '{produit.nom}'.",
                f"{greeting(client.nom)}! Efa azonay ny JP-nao ho an'ny '{produit.nom}'.",
            ])
            message_content = (
                f"{intro} Fa efa misy nanao commande mialoha, ka ao amin'ny liste d'attente "
                f"ianao izao (numéro {ordre_jp}). Hilazanay anao raha vao misy toerana.{emoji(prob=0.4)}"
            )

        message = Message.objects.create(
            commande=commande,
            contenu=message_content,
            numero_relance=0,
        )

        if ordre_jp == 1:
            MessagingService.send_automatic_message(client, produit, commande.id)
        else:
            MessagingService.send_waiting_list_message(client, produit, ordre_jp, commande.id)

        serializer = CommandeSerializer(commande)
        return Response(
            {
                'commande': serializer.data,
                'produit_reconnu': produit.nom,
                'message_envoye': message.contenu,
                'ai_analysis': parsed,
            },
            status=status.HTTP_201_CREATED,
        )

    def extract_product_query(self, text):
        cleaned = text.upper()
        cleaned = re.sub(r'JE\s*PRENDS|JP|JE\s*VOIS', ' ', cleaned)
        cleaned = re.sub(r'–.*$', ' ', cleaned)
        cleaned = re.sub(r'\d+[\s\S]*AR', ' ', cleaned)
        cleaned = re.sub(r'[^A-Z0-9\s]', ' ', cleaned)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        return cleaned

    def find_best_match(self, query, couleur=None, taille=None):
        if not query:
            return None

        code = normalize_jp_code(query)
        if code:
            variante = Variante.objects.filter(code_jp__iexact=code).select_related('produit').first()
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
        return None

    def build_auto_message(self, client, produit):
        demande = pick([
            "Mba alefaso anay azafady ny anaranao, numéro, adresse ary ny daty hanaterana.",
            "Mba hahavita ny commande, omeo anay ny anaranao, numéro, adresse ary ny daty hanaterana.",
        ])
        return (
            f"{greeting(client.nom)} 😊 Voaray ny JP-nao ho an'ny '{produit.nom}'. {demande}{emoji(prob=0.3)}"
        )


def _create_jp_commande(client, produit, live=None):
    """Alias rétrocompatible — voir backend.jp_capture.create_jp_commande."""
    return create_jp_commande(client, produit, live=live)


class JPAnalyseAPIView(APIView):
    permission_classes = [AllowAny]  # MVP — accessible sans token

    def post(self, request):
        comment_text = request.data.get('comment_text', '')
        if not comment_text:
            return Response({'detail': 'Le champ comment_text est requis.'}, status=status.HTTP_400_BAD_REQUEST)

        parsed = JPCommentAnalyzer().analyze(comment_text)
        return Response(parsed, status=status.HTTP_200_OK)


class LivraisonTrackingAPIView(APIView):
    permission_classes = [AllowAny]  # MVP — accessible sans token

    def get(self, request):
        commande_id = request.query_params.get('commande_id')
        queryset = Livraison.objects.select_related('commande__client', 'livreur').all()
        if commande_id:
            livraison = get_object_or_404(queryset, commande__id=commande_id)
            serializer = LivraisonSerializer(livraison)
            return Response(serializer.data)

        serializer = LivraisonSerializer(queryset, many=True)
        return Response(serializer.data)


class TicketAPIView(APIView):
    permission_classes = [AllowAny]  # MVP — accessible sans token

    def get(self, request, commande_id):
        commande = get_object_or_404(
            Commande.objects.select_related('client', 'produit', 'variante').prefetch_related('paiement', 'livraison__livreur'),
            id=commande_id,
        )
        variante_info = _commande_variante_payload(commande)
        ticket = {
            'commande_id': commande.id,
            'client': {
                'nom': commande.client.nom,
                'telephone': commande.client.telephone,
                'adresse': commande.client.adresse,
                'date_livraison_preferee': commande.client.date_livraison_preferee,
            },
            'produit': {
                'nom': commande.produit.nom,
                'taille': variante_info['taille'],
                'couleur': variante_info['couleur'],
                'prix': variante_info['prix'],
                'code_jp': variante_info['code_jp'],
            },
            'statut_commande': commande.get_statut_display(),
            'paiement': {
                'statut': commande.paiement.statut if hasattr(commande, 'paiement') else None,
                'methode': commande.paiement.methode if hasattr(commande, 'paiement') else None,
            },
            'livraison': {
                'statut': commande.livraison.get_statut_display() if hasattr(commande, 'livraison') else None,
                'localisation_actuelle': commande.livraison.localisation_actuelle if hasattr(commande, 'livraison') else None,
                'livreur': commande.livraison.livreur.nom if hasattr(commande, 'livraison') and commande.livraison.livreur else None,
            },
            'ticket_text': (
                f"TICKET COMMANDE #{commande.id}\n"
                f"Client: {commande.client.nom}\n"
                f"Téléphone: {commande.client.telephone}\n"
                f"Adresse: {commande.client.adresse}\n"
                f"Produit: {commande.produit.nom} ({variante_info['couleur']}, {variante_info['taille']})\n"
                f"Prix: {variante_info['prix']} Ar\n"
                f"Statut commande: {commande.get_statut_display()}\n"
                f"Statut livraison: {commande.livraison.get_statut_display() if hasattr(commande, 'livraison') else 'N/A'}\n"
            ),
        }
        return Response(ticket)


class CommandeSearchAPIView(generics.ListAPIView):
    serializer_class = CommandeSerializer
    permission_classes = [AllowAny]  # MVP — accessible sans token

    def get_queryset(self):
        query = self.request.query_params.get('q', '').strip()
        queryset = Commande.objects.select_related('client', 'produit', 'variante').all()
        if not query:
            return queryset.order_by('-date_creation')

        filters = (
            models.Q(client__nom__icontains=query)
            | models.Q(client__telephone__icontains=query)
            | models.Q(produit__nom__icontains=query)
            | models.Q(variante__couleur__icontains=query)
            | models.Q(variante__taille__icontains=query)
            | models.Q(variante__code_jp__icontains=query)
            | models.Q(statut__icontains=query)
        )
        if query.isdigit():
            filters |= models.Q(id=int(query))

        return queryset.filter(filters).order_by('-date_creation')


class JPRelanceAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        force = request.data.get('force', False) or request.query_params.get('force', 'false').lower() == 'true'
        from .jp_relances import process_jp_relances

        result = process_jp_relances(force=bool(force))
        return Response(
            {
                'relances': result['relances'],
                'expirations': result['expirations'],
                'inbox_synced': result['inbox_synced'],
            },
            status=status.HTTP_200_OK,
        )


class CommandeUploadPaiementAPIView(APIView):
    parser_classes = [MultiPartParser, FormParser]
    permission_classes = [AllowAny]  # MVP — accessible sans token

    def post(self, request, pk):
        commande = get_object_or_404(Commande, pk=pk)
        file_obj = request.FILES.get('file')
        if not file_obj:
            return Response(
                {'detail': "Aucun fichier téléversé. Veuillez envoyer le screenshot sous la clé 'file'."},
                status=status.HTTP_400_BAD_REQUEST
            )

        paiement, _created = Paiement.objects.get_or_create(
            commande=commande,
            defaults={
                'methode': Paiement.METHODE_MOBILE_MONEY,
                'statut': Paiement.STATUT_PAYE
            }
        )

        from django.core.files.storage import default_storage
        file_name = f"payments/receipt_{commande.id}_{file_obj.name}"
        saved_path = default_storage.save(file_name, file_obj)

        paiement.methode = Paiement.METHODE_MOBILE_MONEY
        paiement.statut = Paiement.STATUT_PAYE
        paiement.capture_mobile_money = default_storage.url(saved_path)
        paiement.save()

        commande.statut = Commande.STATUT_CONFIRME
        commande.save()

        return Response({
            'detail': "Capture de paiement Mobile Money téléversée avec succès.",
            'paiement': PaiementSerializer(paiement).data,
            'commande_statut': commande.statut
        }, status=status.HTTP_200_OK)


class CommandeEtiquetteJPAPIView(APIView):
    permission_classes = [AllowAny]  # MVP — accessible sans token

    def get(self, request, pk):
        commande = get_object_or_404(Commande.objects.select_related('produit', 'variante'), pk=pk)
        variante = _commande_variante(commande)
        if not variante:
            return Response({'detail': 'Aucune variante associée à cette commande.'}, status=status.HTTP_404_NOT_FOUND)

        bare_code = code_for_commande(commande)
        code_display = format_jp_code(bare_code)
        label_text = (
            f"{code_display} {commande.produit.nom.upper()} - {int(variante.prix_unitaire):,} Ar\n"
            f"({variante.couleur.upper()}, {variante.taille.upper()})"
        )

        ticket_data = {
            'commande_id': commande.id,
            'produit_nom': commande.produit.nom,
            'prix': str(variante.prix_unitaire),
            'couleur': variante.couleur,
            'taille': variante.taille,
            'code_jp': bare_code,
            'ordre_jp': commande.ordre_jp,
            'label_text': label_text,
            'html_print': (
                f"<div style='width: 58mm; font-family: monospace; text-align: center; border: 1px dashed black; padding: 10px; margin: 10px;'>"
                f"<h2>AZLIVE LABEL</h2>"
                f"<div style='font-size: 16px; font-weight: bold; margin: 10px 0;'>{code_display} {commande.produit.nom.upper()}</div>"
                f"<div style='font-size: 20px; font-weight: bold; margin: 5px 0;'>{int(variante.prix_unitaire):,} Ar</div>"
                f"<div style='font-size: 12px; margin: 5px 0;'>Taille: {variante.taille.upper()} | Couleur: {variante.couleur.upper()}</div>"
                f"<div style='font-size: 10px; color: gray; margin-top: 15px;'>Commande #{commande.id} | Ordre JP: #{commande.ordre_jp}</div>"
                f"</div>"
            )
        }
        return Response(ticket_data, status=status.HTTP_200_OK)


class CommandeFacturePDFView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, pk):
        commande = get_object_or_404(
            Commande.objects.select_related('client', 'produit', 'variante'),
            pk=pk,
        )
        pdf_bytes = build_facture_pdf(commande)
        return pdf_response(pdf_bytes, f'facture_commande_{commande.id}.pdf')


class CommandeEtiquetteLivraisonPDFView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, pk):
        commande = get_object_or_404(
            Commande.objects.select_related('client', 'produit', 'variante'),
            pk=pk,
        )
        pdf_bytes = build_etiquette_livraison_pdf(commande)
        return pdf_response(pdf_bytes, f'etiquette_commande_{commande.id}.pdf')


class CommandeConfirmerAPIView(APIView):
    permission_classes = [AllowAny]
    parser_classes = [JSONParser, FormParser]

    def post(self, request, pk):
        commande = get_object_or_404(
            Commande.objects.select_related('client', 'produit', 'variante', 'live'),
            pk=pk,
        )

        if request.data.get('message_text'):
            try:
                channel = request.data.get('channel', 'Manuel')
                id_field = 'tiktok_id' if channel == 'TikTok' else 'facebook_id'
                sender_id = request.data.get('sender_id') or getattr(commande.client, id_field, None)
                if not sender_id:
                    return Response(
                        {'detail': f'Identifiant client {id_field} manquant pour cette commande.'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                result = process_inbound_private_message(
                    sender_id=str(sender_id),
                    message_text=request.data['message_text'],
                    channel=channel,
                    page_id=request.data.get('page_id'),
                    id_field=id_field,
                )
                return Response(result, status=status.HTTP_200_OK)
            except OrderConfirmationError as exc:
                return Response({'detail': exc.message, **exc.payload}, status=exc.status_code)

        from .order_confirmation import _analyzer_client_for_commande

        analyzer_client = (
            _analyzer_client_for_commande(commande)
            if commande.statut == Commande.STATUT_JP_CAPTURE
            else commande.client
        )
        parsed = analyze_confirmation_message(
            request.data.get('message_text', ''),
            client=analyzer_client,
        )
        if not parsed:
            parsed = {
                key: request.data[key]
                for key in ('nom', 'telephone', 'adresse', 'date_livraison', 'heure_livraison')
                if request.data.get(key)
            }

        try:
            result = handle_client_reply(
                commande,
                parsed,
                inbound_text=request.data.get('message_text', ''),
                canal=request.data.get('channel'),
            )
            return Response(result, status=status.HTTP_200_OK)
        except OrderConfirmationError as exc:
            return Response({'detail': exc.message, **exc.payload}, status=exc.status_code)


class CommandeLancerLivraisonAPIView(APIView):
    permission_classes = [AllowAny]  # MVP — accessible sans token

    def post(self, request, pk):
        commande = get_object_or_404(Commande.objects.select_related('client', 'produit__vendeur'), pk=pk)

        if commande.statut in (Commande.STATUT_EN_LIVRAISON, Commande.STATUT_LIVRE):
            return Response(
                {'detail': f"Impossible de lancer la livraison : la commande est déjà en statut '{commande.get_statut_display()}'."},
                status=status.HTTP_409_CONFLICT
            )

        if commande.statut == Commande.STATUT_JP_CAPTURE:
            commande.statut = Commande.STATUT_CONFIRME
            commande.save()

        livraison, created = Livraison.objects.get_or_create(
            commande=commande,
            defaults={
                'statut': Livraison.STATUT_PREPARATION,
                'localisation_actuelle': "Bureau Principal"
            }
        )

        if not livraison.livreur:
            livreur, _ = Livreur.objects.get_or_create(
                nom="Livreur AZExpress Standard",
                defaults={'telephone': '0340000000'}
            )
            livraison.livreur = livreur

        livraison.statut = Livraison.STATUT_EN_LIVRAISON
        livraison.localisation_actuelle = "En cours d'expédition avec AZExpress"
        livraison.date_assignation = timezone.now()
        livraison.save()

        commande.statut = Commande.STATUT_EN_LIVRAISON
        commande.save()

        az_response = AZExpressService.transmettre_colis(commande, livraison)

        livraison.tracking_notes = f"Tracking ID: {az_response.get('tracking_number')}. Estimé le: {az_response.get('estimated_delivery')}"
        livraison.save()

        return Response({
            'detail': "Colis expédié et transmis avec succès à AZExpress.",
            'livraison': LivraisonSerializer(livraison).data,
            'azexpress_response': az_response,
            'commande_statut': commande.statut
        }, status=status.HTTP_200_OK)


class DashboardStatsAPIView(APIView):
    def get(self, request):
        vendeur_id = request.query_params.get('vendeur_id')
        commandes_query = Commande.objects.select_related('produit', 'variante').all()
        lives_query = Live.objects.all()
        products_query = Produit.objects.prefetch_related('variantes').all()

        if request.user.is_authenticated:
            try:
                vendeur = request.user.vendeur
                commandes_query = commandes_query.filter(produit__vendeur=vendeur)
                lives_query = lives_query.filter(vendeur=vendeur)
                products_query = products_query.filter(vendeur=vendeur)
            except Vendeur.DoesNotExist:
                if vendeur_id:
                    commandes_query = commandes_query.filter(produit__vendeur_id=vendeur_id)
                    lives_query = lives_query.filter(vendeur_id=vendeur_id)
                    products_query = products_query.filter(vendeur_id=vendeur_id)
        elif vendeur_id:
            commandes_query = commandes_query.filter(produit__vendeur_id=vendeur_id)
            lives_query = lives_query.filter(vendeur_id=vendeur_id)
            products_query = products_query.filter(vendeur_id=vendeur_id)
        else:
            return Response(
                {'detail': 'Authentification ou paramètre vendeur_id requis pour accéder aux statistiques.'},
                status=status.HTTP_403_FORBIDDEN
            )

        total_jps = commandes_query.count()

        confirmed_orders = commandes_query.filter(
            statut__in=[
                Commande.STATUT_CONFIRME,
                Commande.STATUT_PREPARE,
                Commande.STATUT_EN_LIVRAISON,
                Commande.STATUT_LIVRE
            ]
        )
        confirmed_count = confirmed_orders.count()

        taux_confirmation = (confirmed_count / total_jps * 100) if total_jps > 0 else 0

        chiffre_affaires = sum(float(cmd.get_prix_total()) for cmd in confirmed_orders)

        commission_rate = float(ParametresPlateforme.get_current().taux_commission)
        montant_a_reverser = float(chiffre_affaires) * (1.0 - commission_rate)

        best_sellers = (
            commandes_query.values('produit__nom')
            .annotate(total_ventes=Sum(Coalesce('quantite', Value(1))))
            .order_by('-total_ventes')[:5]
        )
        best_sellers_list = [{'produit_nom': item['produit__nom'], 'ventes': item['total_ventes']} for item in best_sellers]

        lives_realises_count = lives_query.filter(statut=Live.STATUT_TERMINE).count()
        total_stock = sum(v.stock for p in products_query for v in p.variantes.all())

        months = {
            1: 'Janvier', 2: 'Février', 3: 'Mars', 4: 'Avril', 5: 'Mai', 6: 'Juin',
            7: 'Juillet', 8: 'Août', 9: 'Septembre', 10: 'Octobre', 11: 'Novembre', 12: 'Décembre'
        }
        monthly_chart_data = []
        for m_num, m_name in months.items():
            month_orders = confirmed_orders.filter(date_creation__month=m_num)
            revenue = sum(float(cmd.get_prix_total()) for cmd in month_orders)
            monthly_chart_data.append({
                'mois': m_name,
                'chiffre_affaires': float(revenue)
            })

        best_sellers_ranking = []
        best_sellers_query = (
            confirmed_orders.values('variante_id', 'produit_id')
            .annotate(units_sold=Sum(Coalesce('quantite', Value(1))))
            .order_by('-units_sold')[:5]
        )
        for index, item in enumerate(best_sellers_query, start=1):
            variante = Variante.objects.filter(id=item['variante_id']).first() if item['variante_id'] else None
            prod = Produit.objects.filter(id=item['produit_id']).first()
            if prod:
                prix = float(variante.prix_unitaire) if variante else float(prod.variantes.order_by('id').first().prix_unitaire) if prod.variantes.exists() else 0
                stock = variante.stock if variante else prod.stock_total
                raw_code = variante.code_jp if variante else (prod.variantes.order_by('id').first().code_jp if prod.variantes.exists() else '')
                code_jp = normalize_jp_code(raw_code) or f'P{prod.id}'
                best_sellers_ranking.append({
                    'rang': index,
                    'produit_nom': prod.nom,
                    'code_jp': code_jp,
                    'prix_unitaire': prix,
                    'unites_vendues': item['units_sold'],
                    'stock_restant': stock,
                    'revenus_cumules': float(prix * item['units_sold'])
                })

        return Response({
            'chiffre_affaires': float(chiffre_affaires),
            'articles_vendus': confirmed_count,
            'lives_realises': lives_realises_count,
            'articles_en_stock': total_stock,
            'monthly_chart_data': monthly_chart_data,
            'best_sellers_ranking': best_sellers_ranking,
            'nombre_jps': total_jps,
            'confirmes': confirmed_count,
            'taux_confirmation': round(taux_confirmation, 2),
            'montant_a_reverser': round(montant_a_reverser, 2),
            'commission_plateforme': round(float(chiffre_affaires) * commission_rate, 2),
            'produits_les_plus_vendus': best_sellers_list
        }, status=status.HTTP_200_OK)


class LiveListCreateView(generics.ListCreateAPIView):
    serializer_class = LiveSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        queryset = Live.objects.select_related('vendeur').all().order_by('-date_live')
        vendeur_id = self.request.query_params.get('vendeur_id')
        if vendeur_id:
            queryset = queryset.filter(vendeur_id=vendeur_id)
        return queryset

    def list(self, request, *args, **kwargs):
        # ?sync=1 → démarre uniquement les scouts WebSocket (0 REST = 0 quota API).
        if str(request.query_params.get('sync') or '') in {'1', 'true', 'yes'}:
            vendeur_id = request.query_params.get('vendeur_id')
            try:
                from .tiktool_live import ensure_tiktok_scouts

                kwargs_scouts = {}
                if vendeur_id and str(vendeur_id).isdigit():
                    kwargs_scouts['vendeur_id'] = int(vendeur_id)
                ensure_tiktok_scouts(**kwargs_scouts)
            except Exception:
                pass
        return super().list(request, *args, **kwargs)


class LiveDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Live.objects.all()
    serializer_class = LiveSerializer
    permission_classes = [AllowAny]

    def perform_update(self, serializer):
        old_statut = serializer.instance.statut
        live = serializer.save()
        if old_statut != live.statut:
            if live.statut == Live.STATUT_EN_COURS:
                live = demarrer_live(live)
            elif live.statut == Live.STATUT_TERMINE:
                live = arreter_live(live)
            serializer.instance = live


class LiveCodesAPIView(APIView):
    """Gestion des codes JP propres a un live.

    GET  : liste des correspondances code -> variante du live.
    POST : upsert d'une liste {variante_id, code}. Les codes sont uniques DANS le
           live mais reutilisables d'un live a l'autre (sans ecraser les autres lives).
           Un code vide supprime la correspondance de la variante pour ce live.
    """
    permission_classes = [AllowAny]

    def get(self, request, pk):
        live = get_object_or_404(Live, pk=pk)
        codes = live.codes_jp.select_related('variante').all()
        return Response(LiveCodeJPSerializer(codes, many=True).data, status=status.HTTP_200_OK)

    def post(self, request, pk):
        live = get_object_or_404(Live, pk=pk)
        payload = request.data.get('codes', request.data)
        if not isinstance(payload, list):
            return Response(
                {'detail': "Le corps doit contenir une liste 'codes' de {variante_id, code}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Normalisation + detection des doublons de code dans le lot soumis.
        entries = []
        seen_codes = {}
        for item in payload:
            variante_id = item.get('variante_id') or item.get('variante')
            if not variante_id:
                return Response({'detail': 'variante_id manquant.'}, status=status.HTTP_400_BAD_REQUEST)
            code = normalize_jp_code(item.get('code'))
            entries.append((int(variante_id), code))
            if code:
                if code in seen_codes and seen_codes[code] != int(variante_id):
                    return Response(
                        {'detail': f'Le code "{code}" est utilise deux fois dans la requete.'},
                        status=status.HTTP_409_CONFLICT,
                    )
                seen_codes[code] = int(variante_id)

        try:
            with transaction.atomic():
                for variante_id, code in entries:
                    variante = Variante.objects.filter(pk=variante_id).first()
                    if not variante:
                        raise OrderConfirmationError(
                            f'Variante #{variante_id} introuvable.', status_code=404
                        )
                    if not code:
                        LiveCodeJP.objects.filter(live=live, variante=variante).delete()
                        continue
                    # Conflit : meme code deja pris dans CE live par une AUTRE variante.
                    conflict = (
                        LiveCodeJP.objects.filter(live=live, code__iexact=code)
                        .exclude(variante=variante)
                        .exists()
                    )
                    if conflict:
                        raise OrderConfirmationError(
                            f'Le code "{code}" est deja utilise dans ce live.',
                            status_code=409,
                        )
                    LiveCodeJP.objects.update_or_create(
                        live=live, variante=variante, defaults={'code': code}
                    )
        except OrderConfirmationError as exc:
            return Response({'detail': exc.message}, status=exc.status_code)

        codes = live.codes_jp.select_related('variante').all()
        return Response(LiveCodeJPSerializer(codes, many=True).data, status=status.HTTP_200_OK)


class CollaborateurListCreateView(generics.ListCreateAPIView):
    queryset = Collaborateur.objects.all().order_by('nom')
    serializer_class = CollaborateurSerializer
    permission_classes = [AllowAny]


class CollaborateurDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Collaborateur.objects.all()
    serializer_class = CollaborateurSerializer
    permission_classes = [AllowAny]


class VarianteListCreateView(generics.ListCreateAPIView):
    queryset = Variante.objects.select_related('produit').all()
    serializer_class = VarianteSerializer
    permission_classes = [AllowAny]

    def perform_create(self, serializer):
        serializer.save()


class VarianteDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Variante.objects.select_related('produit').all()
    serializer_class = VarianteSerializer
    permission_classes = [AllowAny]


class ClientListCreateView(generics.ListCreateAPIView):
    queryset = Client.objects.all().order_by('nom')
    serializer_class = ClientSerializer
    permission_classes = [AllowAny]


class ClientDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Client.objects.all()
    serializer_class = ClientSerializer
    permission_classes = [AllowAny]


class ClientStatsAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        vendeur_id = request.query_params.get('vendeur_id')
        clients_query = Client.objects.all()
        commandes_query = Commande.objects.select_related('variante').filter(
            statut__in=[
                Commande.STATUT_CONFIRME,
                Commande.STATUT_PREPARE,
                Commande.STATUT_EN_LIVRAISON,
                Commande.STATUT_LIVRE
            ]
        )

        if request.user.is_authenticated:
            try:
                vendeur = request.user.vendeur
                commandes_query = commandes_query.filter(produit__vendeur=vendeur)
                clients_query = Client.objects.filter(commandes__produit__vendeur=vendeur).distinct()
            except Vendeur.DoesNotExist:
                if vendeur_id:
                    commandes_query = commandes_query.filter(produit__vendeur_id=vendeur_id)
                    clients_query = Client.objects.filter(commandes__produit__vendeur_id=vendeur_id).distinct()
        elif vendeur_id:
            commandes_query = commandes_query.filter(produit__vendeur_id=vendeur_id)
            clients_query = Client.objects.filter(commandes__produit__vendeur_id=vendeur_id).distinct()

        total_clients = clients_query.count()
        avg_order_price = (
            sum(float(cmd.get_prix_total()) for cmd in commandes_query) / commandes_query.count()
            if commandes_query.count() else 0
        )

        client_order_counts = (
            commandes_query.values('client')
            .annotate(cnt=Count('id'))
            .filter(cnt__gte=2)
        )
        fideles_count = client_order_counts.count()
        taux_fidelite = (fideles_count / total_clients * 100) if total_clients > 0 else 0

        return Response({
            'nombre_clients': total_clients,
            'prix_moyen_commande': round(float(avg_order_price), 2),
            'taux_fidelite': round(taux_fidelite, 2),
            'clients_fideles_count': fideles_count
        }, status=status.HTTP_200_OK)


class SocialConnectAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        vendeur_id = request.data.get('vendeur_id')
        platform = request.data.get('platform')

        if not vendeur_id:
            return Response({'detail': 'Le champ vendeur_id est requis.'}, status=status.HTTP_400_BAD_REQUEST)

        vendeur = get_object_or_404(Vendeur, id=vendeur_id)

        if platform == 'facebook':
            if facebook_configured() and vendeur.facebook_access_token:
                try:
                    sync_vendeur_pages(vendeur)
                except FacebookOAuthError as exc:
                    return Response({'detail': exc.message}, status=exc.status_code)
            elif facebook_configured() and not vendeur.facebook_access_token:
                return Response(
                    {
                        'detail': (
                            'Connectez-vous d\'abord via Facebook '
                            '(POST /api/auth/facebook/token/ ou le flux OAuth).'
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            else:
                vendeur.facebook_page_id = request.data.get('facebook_page_id', 'fb_page_123456789')
                vendeur.facebook_page_name = request.data.get('facebook_page_name', 'Ma Boutique Facebook Officielle')
                vendeur.is_demo_mode = False

                pages_to_create = [
                    {'page_id': 'fb_page_123', 'nom': 'AZLive Fashion'},
                    {'page_id': 'fb_page_456', 'nom': 'Boutique Chic Madagascar'},
                    {'page_id': 'fb_page_789', 'nom': 'Tana Dressing Hub'},
                    {'page_id': 'fb_page_999', 'nom': "L'armoire des Princesses"},
                ]
                for p in pages_to_create:
                    PageFacebook.objects.get_or_create(
                        vendeur=vendeur,
                        page_id=p['page_id'],
                        defaults={'nom': p['nom'], 'statut': PageFacebook.STATUT_PRET}
                    )
        elif platform == 'tiktok':
            from .jp_capture import normalize_tiktok_username

            raw = (request.data.get('tiktok_username') or '').strip()
            if not raw:
                return Response(
                    {
                        'detail': (
                            'Indiquez votre @TikTok (unique_id, ex. azplus.mg). '
                            'Le nom d\'affichage (emoji) ne permet pas de détecter un live.'
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            handle = normalize_tiktok_username(raw)
            if not re.fullmatch(r'[a-z0-9._-]+', handle):
                return Response(
                    {
                        'detail': (
                            f'@{handle} n\'est pas un @TikTok valide. '
                            'Utilisez le handle de votre profil (lettres, chiffres, . _ -), '
                            'pas le nom d\'affichage.'
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            vendeur.tiktok_username = f'@{handle}'
            vendeur.is_demo_mode = False
        elif platform == 'demo':
            vendeur.is_demo_mode = True
            vendeur.facebook_page_id = None
            vendeur.facebook_page_name = None
            vendeur.tiktok_username = None
        else:
            return Response({'detail': 'Plateforme invalide.'}, status=status.HTTP_400_BAD_REQUEST)

        vendeur.save()
        if platform == 'tiktok':
            try:
                from .tiktool_live import ensure_tiktok_scouts

                ensure_tiktok_scouts(vendeur_id=vendeur.pk)
            except Exception:
                pass
        return Response(VendeurSerializer(vendeur).data, status=status.HTTP_200_OK)


class SocialDisconnectAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        vendeur_id = request.data.get('vendeur_id')
        platform = request.data.get('platform')

        if not vendeur_id:
            return Response({'detail': 'Le champ vendeur_id est requis.'}, status=status.HTTP_400_BAD_REQUEST)

        vendeur = get_object_or_404(Vendeur, id=vendeur_id)

        if platform == 'facebook' or platform == 'all':
            vendeur.facebook_page_id = None
            vendeur.facebook_page_name = None
            vendeur.facebook_user_id = None
            vendeur.facebook_access_token = None
            vendeur.pages_facebook.all().delete()
        if platform == 'tiktok' or platform == 'all':
            vendeur.tiktok_username = None
            vendeur.tiktok_open_id = None
            vendeur.tiktok_access_token = None
            vendeur.tiktok_refresh_token = None
        if platform == 'demo' or platform == 'all':
            vendeur.is_demo_mode = False

        vendeur.save()
        return Response(VendeurSerializer(vendeur).data, status=status.HTTP_200_OK)


class FacebookPagesAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        vendeur_id = request.query_params.get('vendeur_id')
        if not vendeur_id and request.user.is_authenticated:
            try:
                vendeur_id = request.user.vendeur.id
            except Vendeur.DoesNotExist:
                pass

        if vendeur_id:
            pages = PageFacebook.objects.filter(vendeur_id=vendeur_id)
        else:
            pages = PageFacebook.objects.all()

        serializer = PageFacebookSerializer(pages, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class ParametresPlateformeAPIView(APIView):
    """Paramètres globaux (timeout file JP TikTok, commission…)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        params = ParametresPlateforme.get_current()
        return Response(ParametresPlateformeSerializer(params).data)

    def patch(self, request):
        params = ParametresPlateforme.get_current()
        serializer = ParametresPlateformeSerializer(params, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)
