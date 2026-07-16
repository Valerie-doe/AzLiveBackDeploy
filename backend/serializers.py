from rest_framework import serializers

from .models import (
    Client,
    Commande,
    Livraison,
    Livreur,
    Paiement,
    Produit,
    ProduitImage,
    Vendeur,
    Message,
    Collaborateur,
    Live,
    LiveCodeJP,
    Variante,
    PageFacebook,
    ParametresPlateforme,
)
from .validators import validate_code_jp_uniqueness, validate_variante_uniqueness


class PageFacebookSerializer(serializers.ModelSerializer):
    class Meta:
        model = PageFacebook
        fields = ['id', 'page_id', 'nom', 'statut', 'webhook_subscribed']


class VendeurSerializer(serializers.ModelSerializer):
    pages_facebook = PageFacebookSerializer(many=True, read_only=True)

    class Meta:
        model = Vendeur
        fields = [
            'id', 'nom', 'contact', 'user', 'facebook_page_id', 'facebook_page_name',
            'tiktok_username', 'is_demo_mode', 'pages_facebook'
        ]


class CollaborateurSerializer(serializers.ModelSerializer):
    class Meta:
        model = Collaborateur
        fields = ['id', 'nom', 'telephone', 'role', 'vendeur']


class VarianteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Variante
        fields = ['id', 'produit', 'taille', 'couleur', 'prix_unitaire', 'stock', 'code_jp']
        read_only_fields = ['produit']

    def validate(self, attrs):
        produit = attrs.get('produit') or getattr(self.instance, 'produit', None)
        taille = attrs.get('taille', getattr(self.instance, 'taille', None))
        couleur = attrs.get('couleur', getattr(self.instance, 'couleur', None))
        code_jp = attrs.get('code_jp', getattr(self.instance, 'code_jp', None))

        if produit and taille and couleur:
            validate_variante_uniqueness(produit, taille, couleur, exclude_pk=getattr(self.instance, 'pk', None))
        if code_jp is not None:
            validate_code_jp_uniqueness(code_jp, produit=produit, exclude_pk=getattr(self.instance, 'pk', None))
        return attrs


class VarianteNestedSerializer(serializers.ModelSerializer):
    """Serializer imbriqué pour création/mise à jour de variantes via Produit."""

    id = serializers.IntegerField(required=False)

    class Meta:
        model = Variante
        fields = ['id', 'taille', 'couleur', 'prix_unitaire', 'stock', 'code_jp']

    def validate(self, attrs):
        produit = self.context.get('produit')
        taille = attrs.get('taille', getattr(self.instance, 'taille', None))
        couleur = attrs.get('couleur', getattr(self.instance, 'couleur', None))
        code_jp = attrs.get('code_jp', getattr(self.instance, 'code_jp', None))

        if produit and taille and couleur:
            validate_variante_uniqueness(produit, taille, couleur, exclude_pk=getattr(self.instance, 'pk', None))
        if code_jp is not None:
            validate_code_jp_uniqueness(code_jp, produit=produit, exclude_pk=getattr(self.instance, 'pk', None))
        return attrs


def build_image_absolute_url(image_field, request=None):
    if not image_field:
        return None

    image_value = str(image_field)
    if image_value.startswith(('http://', 'https://')):
        return image_value

    try:
        url = image_field.url
    except (ValueError, AttributeError):
        return None

    if not url:
        return None
    if url.startswith(('http://', 'https://')):
        return url

    if request is not None:
        return request.build_absolute_uri(url)
    return url


class ProduitImageSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = ProduitImage
        fields = ['id', 'image', 'image_url', 'created_at']
        read_only_fields = ['id', 'created_at']

    def get_image_url(self, obj):
        return build_image_absolute_url(obj.image, self.context.get('request'))

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['image'] = data.get('image_url') or data.get('image')
        return data


class ProduitSerializer(serializers.ModelSerializer):
    vendeur = VendeurSerializer(read_only=True)
    vendeur_id = serializers.PrimaryKeyRelatedField(queryset=Vendeur.objects.all(), source='vendeur', write_only=True)
    variantes = VarianteNestedSerializer(many=True, required=False)
    images = ProduitImageSerializer(many=True, read_only=True)
    photo = serializers.SerializerMethodField()
    photo_url = serializers.SerializerMethodField()

    class Meta:
        model = Produit
        fields = ['id', 'nom', 'photo', 'photo_url', 'images', 'vendeur', 'vendeur_id', 'variantes']


class ProduitCompactSerializer(serializers.ModelSerializer):
    variantes = VarianteNestedSerializer(many=True, required=False)
    photo = serializers.SerializerMethodField()
    photo_url = serializers.SerializerMethodField()

    class Meta:
        model = Produit
        fields = ['id', 'nom', 'photo', 'photo_url', 'variantes']

    def _get_primary_image(self, obj):
        # Utilise le cache prefetch_related('images') plutôt que .order_by().first(),
        # qui déclenchait une requête SQL par produit (N+1 sur chaque liste de lives).
        images = sorted(obj.images.all(), key=lambda img: (img.created_at, img.id))
        if images:
            return images[0].image
        return obj.photo

    def get_photo(self, obj):
        return build_image_absolute_url(self._get_primary_image(obj), self.context.get('request'))

    def get_photo_url(self, obj):
        return self.get_photo(obj)

    def _extract_images_payload(self):
        request = self.context.get('request')
        if request is not None:
            uploaded_files = request.FILES.getlist('images')
            if uploaded_files:
                return {'files': uploaded_files}

        if 'images' in self.initial_data:
            images = self.initial_data.get('images')
            if images in (None, ''):
                return []
            if isinstance(images, list):
                return images

        photo_value = self._extract_legacy_photo_value()
        if photo_value is serializers.empty:
            return serializers.empty
        if photo_value in (None, ''):
            return []
        return [photo_value]

    def _extract_legacy_photo_value(self):
        request = self.context.get('request')
        if request is not None and request.FILES.get('photo'):
            return request.FILES.get('photo')

        if 'photo' not in self.initial_data:
            return serializers.empty

        photo = self.initial_data.get('photo')
        if photo in (None, ''):
            return None
        return photo

    def _sync_legacy_photo(self, produit):
        first = produit.images.order_by('created_at', 'id').first()
        produit.photo = first.image if first else None
        produit.save(update_fields=['photo'])

    def _sync_images(self, produit, images_payload):
        if images_payload is serializers.empty:
            return

        if isinstance(images_payload, dict) and images_payload.get('files'):
            for uploaded_file in images_payload['files']:
                ProduitImage.objects.create(produit=produit, image=uploaded_file)
            self._sync_legacy_photo(produit)
            return

        existing_ids = []
        for item in images_payload or []:
            if isinstance(item, str):
                image_obj = ProduitImage.objects.create(produit=produit, image=item)
                existing_ids.append(image_obj.id)
                continue

            if not isinstance(item, dict):
                continue

            image_id = item.get('id')
            image_value = item.get('image') or item.get('url') or item.get('image_url')

            if image_id:
                image_obj = ProduitImage.objects.filter(pk=image_id, produit=produit).first()
                if image_obj:
                    if image_value:
                        image_obj.image = image_value
                        image_obj.save(update_fields=['image'])
                    existing_ids.append(image_obj.id)
                    continue

            if image_value:
                image_obj = ProduitImage.objects.create(produit=produit, image=image_value)
                existing_ids.append(image_obj.id)

        produit.images.exclude(id__in=existing_ids).delete()
        self._sync_legacy_photo(produit)

    def _sync_variantes(self, produit, variantes_data):
        existing_ids = []
        for variante_data in variantes_data:
            variante_id = variante_data.pop('id', None)
            if variante_id:
                variante = Variante.objects.filter(pk=variante_id, produit=produit).first()
                if variante:
                    for attr, value in variante_data.items():
                        setattr(variante, attr, value)
                    variante.save()
                    existing_ids.append(variante.id)
                    continue
            variante = Variante.objects.create(produit=produit, **variante_data)
            existing_ids.append(variante.id)

        produit.variantes.exclude(id__in=existing_ids).delete()

    def _parse_variantes_from_request(self):
        request = self.context.get('request')
        if request is None:
            return []
        raw_variantes = request.data.get('variantes')
        if isinstance(raw_variantes, str):
            import json
            try:
                return json.loads(raw_variantes)
            except json.JSONDecodeError:
                return []
        return []

    def create(self, validated_data):
        variantes_data = validated_data.pop('variantes', []) or self._parse_variantes_from_request()
        images_payload = self._extract_images_payload()
        produit = Produit.objects.create(**validated_data)
        self._sync_images(produit, images_payload)
        if variantes_data:
            self._sync_variantes(produit, variantes_data)
        return produit

    def update(self, instance, validated_data):
        variantes_data = validated_data.pop('variantes', None)
        if variantes_data is None:
            parsed = self._parse_variantes_from_request()
            variantes_data = parsed if parsed else None
        images_payload = self._extract_images_payload()
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        self._sync_images(instance, images_payload)
        if variantes_data is not None:
            self._sync_variantes(instance, variantes_data)
        return instance


def _commande_prix(commande):
    return float(commande.get_prix_total())


class LiveCodeJPSerializer(serializers.ModelSerializer):
    produit_id = serializers.IntegerField(source='variante.produit_id', read_only=True)

    class Meta:
        model = LiveCodeJP
        fields = ['id', 'variante', 'produit_id', 'code']


class LiveSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = Live
        fields = ['id', 'titre', 'statut']


class LiveSerializer(serializers.ModelSerializer):
    chiffre_affaires = serializers.SerializerMethodField()
    nb_fiches = serializers.SerializerMethodField()
    operateur_nom = serializers.SerializerMethodField()
    produits_dressing = ProduitCompactSerializer(many=True, read_only=True)
    produits_dressing_ids = serializers.PrimaryKeyRelatedField(
        queryset=Produit.objects.all(), source='produits_dressing', many=True, write_only=True, required=False
    )
    codes_jp = LiveCodeJPSerializer(many=True, read_only=True)

    class Meta:
        model = Live
        fields = [
            'id', 'titre', 'date_live', 'statut', 'vendeur', 'operateur',
            'chiffre_affaires', 'nb_fiches', 'operateur_nom',
            'produits_dressing', 'produits_dressing_ids', 'codes_jp', 'pages_facebook',
            'diffusion_plateformes', 'date_debut', 'date_fin',
        ]

    def get_chiffre_affaires(self, obj):
        confirmed_status = {
            Commande.STATUT_CONFIRME,
            Commande.STATUT_PREPARE,
            Commande.STATUT_EN_LIVRAISON,
            Commande.STATUT_LIVRE,
        }
        # Utilise le cache prefetch_related('commandes') du queryset (get_queryset) :
        # un .filter()/.count() sur la relation refait sinon une requête SQL par live.
        orders = [c for c in obj.commandes.all() if c.statut in confirmed_status]
        total = sum(_commande_prix(order) for order in orders)
        return float(total)

    def get_nb_fiches(self, obj):
        return len(obj.commandes.all())

    def get_operateur_nom(self, obj):
        return obj.operateur.nom if obj.operateur else None


class ClientSerializer(serializers.ModelSerializer):
    sessions_count = serializers.SerializerMethodField()
    montant_valide = serializers.SerializerMethodField()

    class Meta:
        model = Client
        fields = [
            'id',
            'nom',
            'telephone',
            'adresse',
            'date_livraison_preferee',
            'heure_livraison_preferee',
            'facebook_id',
            'tiktok_id',
            'social_handle',
            'sessions_count',
            'montant_valide',
        ]

    def get_sessions_count(self, obj):
        return obj.commandes.count()

    def get_montant_valide(self, obj):
        confirmed_status = [
            Commande.STATUT_CONFIRME,
            Commande.STATUT_PREPARE,
            Commande.STATUT_EN_LIVRAISON,
            Commande.STATUT_LIVRE,
        ]
        orders = obj.commandes.filter(statut__in=confirmed_status)
        total = sum(_commande_prix(order) for order in orders)
        return float(total)


class PaiementSerializer(serializers.ModelSerializer):
    commande_id = serializers.PrimaryKeyRelatedField(queryset=Commande.objects.all(), source='commande')

    class Meta:
        model = Paiement
        fields = ['id', 'commande_id', 'methode', 'statut', 'capture_mobile_money']


class LivreurSerializer(serializers.ModelSerializer):
    class Meta:
        model = Livreur
        fields = ['id', 'nom', 'telephone']


class LivraisonSerializer(serializers.ModelSerializer):
    commande_id = serializers.PrimaryKeyRelatedField(queryset=Commande.objects.all(), source='commande')
    livreur = LivreurSerializer(read_only=True)
    livreur_id = serializers.PrimaryKeyRelatedField(queryset=Livreur.objects.all(), source='livreur', write_only=True, allow_null=True, required=False)

    class Meta:
        model = Livraison
        fields = [
            'id',
            'commande_id',
            'statut',
            'localisation_actuelle',
            'tracking_notes',
            'date_assignation',
            'date_livraison',
            'updated_at',
            'livreur',
            'livreur_id',
        ]


class CommandeSerializer(serializers.ModelSerializer):
    client = ClientSerializer(read_only=True)
    client_id = serializers.PrimaryKeyRelatedField(queryset=Client.objects.all(), source='client', write_only=True)
    produit = ProduitCompactSerializer(read_only=True)
    produit_id = serializers.PrimaryKeyRelatedField(queryset=Produit.objects.all(), source='produit', write_only=True)
    paiement = PaiementSerializer(read_only=True)
    livraison = LivraisonSerializer(read_only=True)
    live = LiveSummarySerializer(read_only=True)
    live_id = serializers.PrimaryKeyRelatedField(queryset=Live.objects.all(), source='live', write_only=True, allow_null=True, required=False)
    variante = VarianteSerializer(read_only=True)
    variante_id = serializers.PrimaryKeyRelatedField(queryset=Variante.objects.all(), source='variante', write_only=True, allow_null=True, required=False)
    prix_unitaire = serializers.SerializerMethodField()
    prix_total = serializers.SerializerMethodField()

    class Meta:
        model = Commande
        fields = [
            'id',
            'client',
            'client_id',
            'produit',
            'produit_id',
            'ordre_jp',
            'quantite',
            'statut',
            'date_creation',
            'paiement',
            'livraison',
            'live',
            'live_id',
            'variante',
            'variante_id',
            'prix_unitaire',
            'prix_total',
        ]

    def get_prix_unitaire(self, obj):
        return float(obj.get_prix_unitaire())

    def get_prix_total(self, obj):
        return float(obj.get_prix_total())


class MessageSerializer(serializers.ModelSerializer):
    commande_id = serializers.PrimaryKeyRelatedField(queryset=Commande.objects.all(), source='commande')

    class Meta:
        model = Message
        fields = ['id', 'commande_id', 'contenu', 'date_envoi', 'numero_relance']
