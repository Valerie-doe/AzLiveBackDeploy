from django.urls import path
from rest_framework.authtoken import views as token_views

from .views import (
    CommandeDetailView,
    CommandeListCreateView,
    CommandeSearchAPIView,
    JPAnalyseAPIView,
    JPRelanceAPIView,
    JPCaptureAPIView,
    LivraisonTrackingAPIView,
    ProduitDetailView,
    ProduitImageDeleteView,
    ProduitListCreateView,
    TicketAPIView,
    VendeurListCreateView,
    CommandeUploadPaiementAPIView,
    CommandeEtiquetteJPAPIView,
    CommandeFacturePDFView,
    CommandeEtiquetteLivraisonPDFView,
    CommandeConfirmerAPIView,
    CommandeLancerLivraisonAPIView,
    DashboardStatsAPIView,
    LiveListCreateView,
    LiveDetailView,
    LiveCodesAPIView,
    CollaborateurListCreateView,
    CollaborateurDetailView,
    VarianteListCreateView,
    VarianteDetailView,
    ClientListCreateView,
    ClientDetailView,
    ClientStatsAPIView,
    SocialConnectAPIView,
    SocialDisconnectAPIView,
    FacebookPagesAPIView,
    ParametresPlateformeAPIView,
)
from .auth_views import (
    AuthMeAPIView,
    FacebookCallbackAPIView,
    FacebookLoginURLAPIView,
    FacebookSubscribeWebhooksAPIView,
    FacebookSyncPagesAPIView,
    FacebookTokenLoginAPIView,
    TikTokCallbackAPIView,
    TikTokLoginURLAPIView,
    TikTokTokenLoginAPIView,
)
from .webhooks import FacebookWebhookView, TikTokWebhookView
from .live_views import LiveDemarrerAPIView, LiveArreterAPIView
from .media_views import MediaMTXAuthAPIView, MediaMTXWhipProxyAPIView
from .public_form_views import (
    PublicOrderCancelAPIView,
    PublicOrderFormAPIView,
    PublicTikTokCallbackAPIView,
    PublicTikTokLoginAPIView,
)

urlpatterns = [
    # Auth
    path('auth/login/', token_views.obtain_auth_token, name='auth-login'),
    path('auth/me/', AuthMeAPIView.as_view(), name='auth-me'),
    path('auth/facebook/login/', FacebookLoginURLAPIView.as_view(), name='auth-facebook-login'),
    path('auth/facebook/callback/', FacebookCallbackAPIView.as_view(), name='auth-facebook-callback'),
    path('auth/facebook/token/', FacebookTokenLoginAPIView.as_view(), name='auth-facebook-token'),
    path('auth/facebook/sync-pages/', FacebookSyncPagesAPIView.as_view(), name='auth-facebook-sync-pages'),
    path('auth/facebook/subscribe-webhooks/', FacebookSubscribeWebhooksAPIView.as_view(), name='auth-facebook-subscribe-webhooks'),
    path('auth/tiktok/login/', TikTokLoginURLAPIView.as_view(), name='auth-tiktok-login'),
    path('auth/tiktok/callback/', TikTokCallbackAPIView.as_view(), name='auth-tiktok-callback'),
    path('auth/tiktok/token/', TikTokTokenLoginAPIView.as_view(), name='auth-tiktok-token'),

    # Vendeurs & Produits
    path('vendeurs/', VendeurListCreateView.as_view(), name='vendeur-list-create'),
    path('vendeurs/connect/', SocialConnectAPIView.as_view(), name='vendeur-social-connect'),
    path('vendeurs/disconnect/', SocialDisconnectAPIView.as_view(), name='vendeur-social-disconnect'),
    path('vendeurs/facebook-pages/', FacebookPagesAPIView.as_view(), name='vendeur-facebook-pages'),

    path('produits/', ProduitListCreateView.as_view(), name='produit-list-create'),
    path('produits/<int:pk>/', ProduitDetailView.as_view(), name='produit-detail'),
    path('produits/images/<int:pk>/', ProduitImageDeleteView.as_view(), name='produit-image-delete'),
    path('produits/variants/', VarianteListCreateView.as_view(), name='variante-list-create'),
    path('produits/variants/<int:pk>/', VarianteDetailView.as_view(), name='variante-detail'),

    # Clients
    path('clients/', ClientListCreateView.as_view(), name='client-list-create'),
    path('clients/stats/', ClientStatsAPIView.as_view(), name='client-stats'),
    path('clients/<int:pk>/', ClientDetailView.as_view(), name='client-detail'),

    # Lives / Sessions
    path('lives/', LiveListCreateView.as_view(), name='live-list-create'),
    path('lives/<int:pk>/', LiveDetailView.as_view(), name='live-detail'),
    path('lives/<int:pk>/codes/', LiveCodesAPIView.as_view(), name='live-codes'),
    path('lives/<int:pk>/demarrer/', LiveDemarrerAPIView.as_view(), name='live-demarrer'),
    path('lives/<int:pk>/arreter/', LiveArreterAPIView.as_view(), name='live-arreter'),

    # Collaborateurs
    path('collaborateurs/', CollaborateurListCreateView.as_view(), name='collaborateur-list-create'),
    path('collaborateurs/<int:pk>/', CollaborateurDetailView.as_view(), name='collaborateur-detail'),

    # Commandes — routes spécifiques AVANT la route générique <int:pk>/
    path('commandes/', CommandeListCreateView.as_view(), name='commande-list-create'),
    path('commandes/search/', CommandeSearchAPIView.as_view(), name='commande-search'),
    path('commandes/<int:pk>/upload-paiement/', CommandeUploadPaiementAPIView.as_view(), name='commande-upload-paiement'),
    path('commandes/<int:pk>/etiquette-jp/', CommandeEtiquetteJPAPIView.as_view(), name='commande-etiquette-jp'),
    path('commandes/<int:pk>/facture.pdf', CommandeFacturePDFView.as_view(), name='commande-facture-pdf'),
    path('commandes/<int:pk>/etiquette-livraison.pdf', CommandeEtiquetteLivraisonPDFView.as_view(), name='commande-etiquette-livraison-pdf'),
    path('commandes/<int:pk>/confirmer/', CommandeConfirmerAPIView.as_view(), name='commande-confirmer'),
    path('commandes/<int:pk>/lancer-livraison/', CommandeLancerLivraisonAPIView.as_view(), name='commande-lancer-livraison'),
    path('commandes/<int:commande_id>/ticket/', TicketAPIView.as_view(), name='commande-ticket'),
    path('commandes/<int:pk>/', CommandeDetailView.as_view(), name='commande-detail'),

    # Livraisons
    path('livraisons/tracking/', LivraisonTrackingAPIView.as_view(), name='livraison-tracking'),

    # JP
    path('jp-capture/', JPCaptureAPIView.as_view(), name='jp-capture'),
    path('jp-analyze/', JPAnalyseAPIView.as_view(), name='jp-analyze'),
    path('jp-relance/', JPRelanceAPIView.as_view(), name='jp-relance'),

    # Formulaire public de commande (live TikTok — collecte indirecte des infos client)
    path('public/lives/<int:live_id>/tiktok-login/', PublicTikTokLoginAPIView.as_view(), name='public-tiktok-login'),
    path('public/tiktok/callback/', PublicTikTokCallbackAPIView.as_view(), name='public-tiktok-callback'),
    path('public/lives/<int:live_id>/order-form/', PublicOrderFormAPIView.as_view(), name='public-order-form'),
    path(
        'public/lives/<int:live_id>/order-form/cancel/',
        PublicOrderCancelAPIView.as_view(),
        name='public-order-cancel',
    ),

    # Webhooks réseaux sociaux
    path('webhooks/facebook/', FacebookWebhookView.as_view(), name='webhook-facebook'),
    path('webhooks/tiktok/', TikTokWebhookView.as_view(), name='webhook-tiktok'),

    # MediaMTX — authentification + proxy WHIP (contournement 502 domaine public MTX)
    path('media/auth/', MediaMTXAuthAPIView.as_view(), name='media-auth'),
    path('media/whip/<str:path>/', MediaMTXWhipProxyAPIView.as_view(), name='media-whip-proxy'),

    # Dashboard
    path('dashboard/stats/', DashboardStatsAPIView.as_view(), name='dashboard-stats'),

    # Paramètres plateforme
    path('parametres/', ParametresPlateformeAPIView.as_view(), name='parametres-plateforme'),
]

