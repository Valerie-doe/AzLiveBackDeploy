from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone


class Vendeur(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='vendeur', null=True, blank=True)
    nom = models.CharField(max_length=255)
    contact = models.CharField(max_length=255)
    facebook_user_id = models.CharField(max_length=255, blank=True, null=True, unique=True)
    facebook_access_token = models.TextField(blank=True, null=True)
    facebook_page_id = models.CharField(max_length=255, blank=True, null=True)
    facebook_page_name = models.CharField(max_length=255, blank=True, null=True)
    tiktok_username = models.CharField(max_length=255, blank=True, null=True)
    tiktok_open_id = models.CharField(max_length=255, blank=True, null=True, unique=True)
    tiktok_access_token = models.TextField(blank=True, null=True)
    tiktok_refresh_token = models.TextField(blank=True, null=True)
    is_demo_mode = models.BooleanField(default=False)

    def __str__(self):
        return self.nom


class PageFacebook(models.Model):
    """Pages Facebook managées par un vendeur, chargées dynamiquement après connexion OAuth."""
    STATUT_PRET = 'pret'
    STATUT_INACTIF = 'inactif'
    STATUT_CHOICES = [
        (STATUT_PRET, 'Prêt !'),
        (STATUT_INACTIF, 'Inactif'),
    ]

    vendeur = models.ForeignKey(Vendeur, on_delete=models.CASCADE, related_name='pages_facebook')
    page_id = models.CharField(max_length=255)          # ID Facebook Graph API
    nom = models.CharField(max_length=255)
    statut = models.CharField(max_length=20, choices=STATUT_CHOICES, default=STATUT_PRET)
    access_token = models.TextField(blank=True, null=True)  # Token page (stocké chiffré en prod)
    webhook_subscribed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('vendeur', 'page_id')

    def __str__(self):
        return f"{self.nom} ({self.vendeur.nom})"


class ParametresPlateforme(models.Model):
    """Paramètres globaux de la plateforme — un seul enregistrement attendu (singleton)."""
    taux_commission = models.DecimalField(
        max_digits=5, decimal_places=4, default=0.10,
        help_text="Taux de commission prélevé par la plateforme (ex: 0.10 = 10%)"
    )
    nom_plateforme = models.CharField(max_length=100, default='AZLive')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Paramètres Plateforme'
        verbose_name_plural = 'Paramètres Plateforme'

    def __str__(self):
        return f"Commission: {self.taux_commission * 100:.1f}%"

    @classmethod
    def get_current(cls):
        """Retourne les paramètres actifs ou crée les valeurs par défaut."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class Collaborateur(models.Model):
    nom = models.CharField(max_length=255)
    telephone = models.CharField(max_length=20, blank=True)
    role = models.CharField(max_length=50, default='operateur')
    vendeur = models.ForeignKey(Vendeur, on_delete=models.CASCADE, related_name='collaborateurs')

    def __str__(self):
        return self.nom


class Live(models.Model):
    STATUT_PLANIFIE = 'planifie'
    STATUT_EN_COURS = 'en_cours'
    STATUT_TERMINE = 'termine'

    STATUT_CHOICES = [
        (STATUT_PLANIFIE, 'Planifié'),
        (STATUT_EN_COURS, 'En cours'),
        (STATUT_TERMINE, 'Terminé'),
    ]

    titre = models.CharField(max_length=255)
    date_live = models.DateTimeField(default=timezone.now)
    statut = models.CharField(max_length=50, choices=STATUT_CHOICES, default=STATUT_PLANIFIE)
    vendeur = models.ForeignKey(Vendeur, on_delete=models.CASCADE, related_name='lives')
    operateur = models.ForeignKey(Collaborateur, on_delete=models.SET_NULL, null=True, blank=True, related_name='lives')
    produits_dressing = models.ManyToManyField('Produit', blank=True, related_name='lives_dressing')
    pages_facebook = models.JSONField(default=list, blank=True, null=True)
    diffusion_plateformes = models.JSONField(default=dict, blank=True)
    date_debut = models.DateTimeField(null=True, blank=True)
    date_fin = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.titre


class Produit(models.Model):
    nom = models.CharField(max_length=255)
    photo = models.ImageField(upload_to='produits/', blank=True, null=True)
    vendeur = models.ForeignKey(Vendeur, on_delete=models.CASCADE, related_name='produits')

    def __str__(self):
        return self.nom

    @property
    def stock_total(self):
        return sum(v.stock for v in self.variantes.all())


class ProduitImage(models.Model):
    produit = models.ForeignKey(Produit, on_delete=models.CASCADE, related_name='images')
    image = models.ImageField(upload_to='produits/')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'id']

    def __str__(self):
        return f"Image #{self.pk} — {self.produit.nom}"


class Variante(models.Model):
    produit = models.ForeignKey(Produit, on_delete=models.CASCADE, related_name='variantes')
    taille = models.CharField(max_length=50)
    couleur = models.CharField(max_length=50)
    prix_unitaire = models.DecimalField(max_digits=10, decimal_places=2)
    stock = models.IntegerField(default=0)
    # Code « catalogue » par défaut (nu, sans préfixe « JP »). N'est plus unique
    # globalement : l'unicité réelle est portée par live via LiveCodeJP. Sert de repli
    # quand un live n'a pas attribué de code spécifique (ou hors live).
    code_jp = models.CharField(max_length=50, blank=True, default='')

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['produit', 'taille', 'couleur'],
                name='unique_produit_taille_couleur',
            ),
        ]

    def __str__(self):
        return f"{self.produit.nom} - {self.couleur} - {self.taille} ({self.code_jp})"

    def clean(self):
        from .jp_codes import normalize_jp_code
        from .validators import validate_code_jp_uniqueness, validate_variante_uniqueness

        self.code_jp = normalize_jp_code(self.code_jp)
        validate_variante_uniqueness(self.produit, self.taille, self.couleur, exclude_pk=self.pk)
        validate_code_jp_uniqueness(self.code_jp, produit=self.produit, exclude_pk=self.pk)

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


class LiveCodeJP(models.Model):
    """Correspondance code JP -> variante, propre à un live.

    Un même code est unique à l'intérieur d'un live mais réutilisable d'un live à
    l'autre (sans écraser les autres lives). Le code est stocké nu (sans préfixe « JP »).
    """
    live = models.ForeignKey('Live', on_delete=models.CASCADE, related_name='codes_jp')
    variante = models.ForeignKey(Variante, on_delete=models.CASCADE, related_name='codes_live')
    code = models.CharField(max_length=50)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['live', 'code'], name='unique_code_per_live'),
            models.UniqueConstraint(fields=['live', 'variante'], name='unique_variante_per_live'),
        ]

    def __str__(self):
        return f"Live #{self.live_id} — {self.code} → {self.variante_id}"

    def clean(self):
        from .jp_codes import normalize_jp_code

        self.code = normalize_jp_code(self.code)
        if not self.code:
            from django.core.exceptions import ValidationError

            raise ValidationError('Le code JP est obligatoire.')

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


class Client(models.Model):
    nom = models.CharField(max_length=255)
    telephone = models.CharField(max_length=20)
    adresse = models.TextField()
    date_livraison_preferee = models.DateField(blank=True, null=True)
    heure_livraison_preferee = models.TimeField(blank=True, null=True)
    facebook_id = models.CharField(max_length=255, blank=True, null=True)
    tiktok_id = models.CharField(max_length=255, blank=True, null=True)
    social_handle = models.CharField(max_length=255, blank=True, null=True)

    def __str__(self):
        return self.nom


class Commande(models.Model):
    STATUT_JP_CAPTURE = 'jp_capture'
    STATUT_CONFIRME = 'confirme'
    STATUT_PREPARE = 'prepare'
    STATUT_EN_LIVRAISON = 'en_livraison'
    STATUT_LIVRE = 'livre'
    STATUT_ANNULE = 'annule'

    STATUT_CHOICES = [
        (STATUT_JP_CAPTURE, 'JP capturé'),
        (STATUT_CONFIRME, 'Confirmé'),
        (STATUT_PREPARE, 'Préparé'),
        (STATUT_EN_LIVRAISON, 'En livraison'),
        (STATUT_LIVRE, 'Livré'),
        (STATUT_ANNULE, 'Annulé'),
    ]

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='commandes')
    produit = models.ForeignKey(Produit, on_delete=models.CASCADE, related_name='commandes')
    ordre_jp = models.IntegerField(default=1)
    # null = le client n'a pas encore indiqué la quantité (demandée pendant la collecte d'infos).
    quantite = models.PositiveIntegerField(null=True, blank=True, default=None)
    statut = models.CharField(max_length=50, choices=STATUT_CHOICES, default=STATUT_JP_CAPTURE)
    date_creation = models.DateTimeField(auto_now_add=True)
    live = models.ForeignKey(Live, on_delete=models.SET_NULL, null=True, blank=True, related_name='commandes')
    variante = models.ForeignKey(Variante, on_delete=models.SET_NULL, null=True, blank=True, related_name='commandes')

    class Meta:
        ordering = ['date_creation']

    @property
    def quantite_effective(self) -> int:
        """Quantité utilisable pour les calculs (1 tant que le client n'a rien indiqué)."""
        return self.quantite or 1

    def _promote_next_in_queue(self):
        """Une place s'est libérée : avance la file (confirme les suivants complets, sinon relance)."""
        from .order_confirmation import promote_queue

        promote_queue(self.produit, variante=self.variante, exclude_pk=self.pk)

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        old_status = None
        if not is_new:
            try:
                old_status = Commande.objects.get(pk=self.pk).statut
            except Commande.DoesNotExist:
                pass

        super().save(*args, **kwargs)

        # Decrement stock if transitioning to Confirmed
        if (is_new and self.statut == self.STATUT_CONFIRME) or (old_status != self.STATUT_CONFIRME and self.statut == self.STATUT_CONFIRME):
            self._adjust_variante_stock(-self.quantite_effective)

        # Increment stock if transitioning from Confirmed to Cancelled
        elif old_status == self.STATUT_CONFIRME and self.statut == self.STATUT_ANNULE:
            self._adjust_variante_stock(self.quantite_effective)

        # Queue Promotion Logic!
        if not is_new and old_status != self.STATUT_ANNULE and self.statut == self.STATUT_ANNULE:
            self._promote_next_in_queue()

    def _get_stock_variante(self):
        if self.variante_id:
            return self.variante
        return self.produit.variantes.order_by('id').first()

    def _adjust_variante_stock(self, delta):
        variante = self._get_stock_variante()
        if variante and variante.stock + delta >= 0:
            variante.stock += delta
            variante.save(update_fields=['stock'])

    def get_prix_unitaire(self):
        if self.variante_id:
            return self.variante.prix_unitaire
        first = self.produit.variantes.order_by('id').first()
        return first.prix_unitaire if first else 0

    def get_prix_total(self):
        return self.get_prix_unitaire() * self.quantite_effective

    def delete(self, *args, **kwargs):
        if self.statut == self.STATUT_CONFIRME:
            self._adjust_variante_stock(self.quantite_effective)
        produit, variante, pk = self.produit, self.variante, self.pk
        super().delete(*args, **kwargs)
        from .order_confirmation import promote_queue

        promote_queue(produit, variante=variante, exclude_pk=pk)

    def __str__(self):
        return f"Commande #{self.pk} - {self.client.nom} - {self.produit.nom}"


class Paiement(models.Model):
    METHODE_LIVRAISON = 'livraison'
    METHODE_MOBILE_MONEY = 'mobile_money'

    STATUT_NON_PAYE = 'non_paye'
    STATUT_PAYE = 'paye'

    METHODE_CHOICES = [
        (METHODE_LIVRAISON, 'Paiement à la livraison'),
        (METHODE_MOBILE_MONEY, 'Mobile Money'),
    ]

    STATUT_CHOICES = [
        (STATUT_NON_PAYE, 'Non payé'),
        (STATUT_PAYE, 'Payé'),
    ]

    commande = models.OneToOneField(Commande, on_delete=models.CASCADE, related_name='paiement')
    methode = models.CharField(max_length=50, choices=METHODE_CHOICES, default=METHODE_LIVRAISON)
    statut = models.CharField(max_length=50, choices=STATUT_CHOICES, default=STATUT_NON_PAYE)
    capture_mobile_money = models.CharField(max_length=500, blank=True)

    def __str__(self):
        return f"Paiement commande #{self.commande.pk} - {self.get_statut_display()}"


class Livreur(models.Model):
    nom = models.CharField(max_length=255)
    telephone = models.CharField(max_length=20)

    def __str__(self):
        return self.nom


class Livraison(models.Model):
    STATUT_BUREAU = 'au_bureau'
    STATUT_PREPARATION = 'en_preparation'
    STATUT_ASSIGNE = 'assigne_livreur'
    STATUT_EN_LIVRAISON = 'en_livraison'
    STATUT_LIVRE = 'livre'

    STATUT_CHOICES = [
        (STATUT_BUREAU, 'Au bureau'),
        (STATUT_PREPARATION, 'En préparation'),
        (STATUT_ASSIGNE, 'Assigné livreur'),
        (STATUT_EN_LIVRAISON, 'En livraison'),
        (STATUT_LIVRE, 'Livré'),
    ]

    commande = models.OneToOneField(Commande, on_delete=models.CASCADE, related_name='livraison')
    statut = models.CharField(max_length=50, choices=STATUT_CHOICES, default=STATUT_BUREAU)
    localisation_actuelle = models.CharField(max_length=255, blank=True)
    tracking_notes = models.TextField(blank=True)
    date_assignation = models.DateTimeField(blank=True, null=True)
    date_livraison = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)
    livreur = models.ForeignKey(Livreur, on_delete=models.SET_NULL, blank=True, null=True, related_name='livraisons')

    def __str__(self):
        return f"Livraison commande #{self.commande.pk} - {self.get_statut_display()}"


class Message(models.Model):
    DIRECTION_OUTBOUND = 'outbound'
    DIRECTION_INBOUND = 'inbound'
    DIRECTION_CHOICES = [
        (DIRECTION_OUTBOUND, 'Sortant'),
        (DIRECTION_INBOUND, 'Entrant'),
    ]

    CANAL_FACEBOOK = 'Facebook'
    CANAL_TIKTOK = 'TikTok'
    CANAL_WHATSAPP = 'WhatsApp'
    CANAL_MOCK = 'Mock'

    commande = models.ForeignKey(Commande, on_delete=models.CASCADE, related_name='messages')
    contenu = models.TextField()
    date_envoi = models.DateTimeField(auto_now_add=True)
    numero_relance = models.IntegerField(default=0)
    direction = models.CharField(
        max_length=20,
        choices=DIRECTION_CHOICES,
        default=DIRECTION_OUTBOUND,
    )
    canal = models.CharField(max_length=50, blank=True, default='')

    def __str__(self):
        return f"Message commande #{self.commande.pk} - relance {self.numero_relance}"
