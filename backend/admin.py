from django.contrib import admin

from .models import Client, Commande, Livraison, Livreur, Paiement, Produit, ProduitImage, Vendeur, Message, Collaborateur, Live, LiveCodeJP, Variante


@admin.register(Vendeur)
class VendeurAdmin(admin.ModelAdmin):
    list_display = ('nom', 'contact', 'facebook_page_name', 'tiktok_username', 'is_demo_mode')



class ProduitImageInline(admin.TabularInline):
    model = ProduitImage
    extra = 1


@admin.register(Produit)
class ProduitAdmin(admin.ModelAdmin):
    list_display = ('nom', 'vendeur')
    list_filter = ('vendeur',)
    search_fields = ('nom',)
    inlines = [ProduitImageInline]


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ('nom', 'telephone', 'adresse', 'date_livraison_preferee')
    search_fields = ('nom', 'telephone', 'adresse')


@admin.register(Commande)
class CommandeAdmin(admin.ModelAdmin):
    list_display = ('id', 'client', 'produit', 'quantite', 'ordre_jp', 'statut', 'date_creation')
    list_filter = ('statut',)
    search_fields = ('client__nom', 'produit__nom')


@admin.register(Paiement)
class PaiementAdmin(admin.ModelAdmin):
    list_display = ('commande', 'methode', 'statut')
    list_filter = ('statut', 'methode')


@admin.register(Livreur)
class LivreurAdmin(admin.ModelAdmin):
    list_display = ('nom', 'telephone')


@admin.register(Livraison)
class LivraisonAdmin(admin.ModelAdmin):
    list_display = ('commande', 'statut', 'livreur', 'date_assignation', 'date_livraison')
    list_filter = ('statut',)


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ('commande', 'date_envoi', 'numero_relance')
    search_fields = ('commande__client__nom', 'contenu')


@admin.register(Collaborateur)
class CollaborateurAdmin(admin.ModelAdmin):
    list_display = ('nom', 'role', 'vendeur')
    list_filter = ('role', 'vendeur')


@admin.register(Live)
class LiveAdmin(admin.ModelAdmin):
    list_display = ('titre', 'date_live', 'statut', 'vendeur', 'operateur')
    list_filter = ('statut', 'vendeur')


@admin.register(Variante)
class VarianteAdmin(admin.ModelAdmin):
    list_display = ('produit', 'code_jp', 'taille', 'couleur', 'prix_unitaire', 'stock')
    list_filter = ('taille', 'couleur')
    search_fields = ('code_jp', 'produit__nom')


@admin.register(LiveCodeJP)
class LiveCodeJPAdmin(admin.ModelAdmin):
    list_display = ('live', 'code', 'variante')
    list_filter = ('live',)
    search_fields = ('code', 'variante__produit__nom')

