from django.test import TestCase, override_settings
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.authtoken.models import Token
from unittest.mock import patch

from .models import Client, Commande, Livraison, Livreur, Paiement, Produit, Vendeur, Message, Variante, Live, PageFacebook
from .jp_capture import resolve_vendeur_from_tiktok_username
from .tiktool_live import process_tiktool_chat_event


def create_test_produit(vendeur, **kwargs):
    defaults = {
        'nom': 'Robe Rouge',
        'photo': None,
    }
    defaults.update(kwargs)
    produit = Produit.objects.create(vendeur=vendeur, **{k: v for k, v in defaults.items() if k != 'variante'})
    variante_defaults = defaults.get('variante', {
        'taille': 'M',
        'couleur': 'Rouge',
        'prix_unitaire': '45000.00',
        'stock': 10,
        'code_jp': f'JP{produit.id}',
    })
    Variante.objects.create(produit=produit, **variante_defaults)
    return produit


class BackendModelsTest(TestCase):
    def test_create_produit_client_commande(self):
        vendeur = Vendeur.objects.create(nom='Vendeur Live', contact='0341234567')
        produit = create_test_produit(vendeur)
        variante = produit.variantes.first()
        client = Client.objects.create(
            nom='Marie', telephone='0349876543', adresse='Antananarivo', date_livraison_preferee='2026-05-20'
        )
        commande = Commande.objects.create(client=client, produit=produit, variante=variante, ordre_jp=1)

        self.assertEqual(commande.client, client)
        self.assertEqual(commande.produit, produit)
        self.assertEqual(commande.statut, Commande.STATUT_JP_CAPTURE)
        self.assertEqual(str(commande), f"Commande #{commande.pk} - {client.nom} - {produit.nom}")

    def test_paiement_livraison_relations(self):
        vendeur = Vendeur.objects.create(nom='Vendeur Live', contact='0341234567')
        produit = create_test_produit(vendeur)
        variante = produit.variantes.first()
        client = Client.objects.create(nom='Jean', telephone='0347654321', adresse='Antananarivo', date_livraison_preferee='2026-05-21')
        commande = Commande.objects.create(client=client, produit=produit, variante=variante, ordre_jp=2)
        paiement = Paiement.objects.create(commande=commande, methode=Paiement.METHODE_LIVRAISON, statut=Paiement.STATUT_NON_PAYE)
        livreur = Livreur.objects.create(nom='Livreur AZExpress', telephone='0331239876')
        livraison = Livraison.objects.create(commande=commande, statut=Livraison.STATUT_ASSIGNE, livreur=livreur)
        message = Message.objects.create(commande=commande, contenu='Merci, envoyez votre adresse.', numero_relance=0)

        self.assertEqual(paiement.commande, commande)
        self.assertEqual(livraison.commande, commande)
        self.assertEqual(livraison.livreur, livreur)
        self.assertEqual(commande.paiement, paiement)
        self.assertEqual(commande.livraison, livraison)
        self.assertEqual(commande.messages.count(), 1)
        self.assertEqual(message.numero_relance, 0)
        self.assertEqual(str(paiement), f"Paiement commande #{commande.pk} - {paiement.get_statut_display()}")


class BackendAPITest(TestCase):
    def setUp(self):
        self.vendeur = Vendeur.objects.create(nom='Vendeur Live', contact='0341234567')
        self.produit = create_test_produit(self.vendeur)
        self.variante = self.produit.variantes.first()

    def test_jp_capture_endpoint_creates_commande(self):
        payload = {
            'comment_text': 'JP ROBE ROUGE',
            'nom': 'Claire',
            'telephone': '0341122334',
            'adresse': 'Antananarivo',
            'date_livraison_preferee': '2026-05-25',
        }
        response = self.client.post('/api/jp-capture/', payload, content_type='application/json')

        self.assertEqual(response.status_code, 201)
        self.assertIn('commande', response.json())
        self.assertEqual(response.json()['produit_reconnu'], 'Robe Rouge')
        self.assertTrue('message_envoye' in response.json())
        self.assertEqual(Commande.objects.count(), 1)
        self.assertEqual(Client.objects.count(), 1)

    def test_produit_list_endpoint(self):
        response = self.client.get('/api/produits/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        results = data['results']
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['nom'], 'Robe Rouge')
        self.assertEqual(len(results[0]['variantes']), 1)

    def test_commande_list_endpoint_returns_lightweight_nested_payload(self):
        live = Live.objects.create(titre='Live test', vendeur=self.vendeur, statut=Live.STATUT_EN_COURS)
        client = Client.objects.create(nom='Lova', telephone='0341234567', adresse='Tana')
        commande = Commande.objects.create(
            client=client,
            produit=self.produit,
            variante=self.variante,
            live=live,
            ordre_jp=1,
        )

        response = self.client.get(f'/api/commandes/?live_id={live.id}')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        results = data['results']
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['id'], commande.id)
        self.assertNotIn('vendeur', results[0]['produit'])
        self.assertNotIn('images', results[0]['produit'])
        self.assertEqual(results[0]['live']['id'], live.id)
        self.assertEqual(results[0]['live']['titre'], 'Live test')

    def test_commande_search_endpoint(self):
        client = Client.objects.create(nom='Serge', telephone='0344455667', adresse='Tananarive')
        commande = Commande.objects.create(client=client, produit=self.produit, variante=self.variante, ordre_jp=1)

        response = self.client.get('/api/commandes/search/', {'q': 'Serge'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        results = data['results']
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['id'], commande.id)

        response = self.client.get('/api/commandes/search/', {'q': 'Robe'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['results']), 1)

    def test_jp_relance_endpoint(self):
        client = Client.objects.create(nom='Emilie', telephone='0349988776', adresse='Tana')
        commande = Commande.objects.create(client=client, produit=self.produit, variante=self.variante, ordre_jp=1)
        Message.objects.create(commande=commande, contenu='Bonjour, merci pour votre JP.', numero_relance=0)

        response = self.client.post('/api/jp-relance/', {'force': True}, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data['relances']), 1)
        self.assertEqual(data['relances'][0]['commande_id'], commande.id)
        self.assertEqual(data['relances'][0]['numero_relance'], 1)

    def test_jp_analyze_endpoint(self):
        payload = {'comment_text': 'JP ROBE ROUGE taille M couleur rouge'}
        response = self.client.post('/api/jp-analyze/', payload, content_type='application/json')

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['intent'], 'achat')
        self.assertIn('product_query', data)
        self.assertEqual(data['produit_trouve'], 'Robe Rouge')

    def test_ticket_endpoint_returns_ticket_data(self):
        client = Client.objects.create(nom='Hery', telephone='0345566778', adresse='Tana')
        commande = Commande.objects.create(client=client, produit=self.produit, variante=self.variante, ordre_jp=1)
        livraison = Livraison.objects.create(commande=commande, statut=Livraison.STATUT_ASSIGNE)

        response = self.client.get(f'/api/commandes/{commande.id}/ticket/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['commande_id'], commande.id)
        self.assertEqual(data['client']['nom'], 'Hery')
        self.assertEqual(data['produit']['nom'], 'Robe Rouge')
        self.assertEqual(data['livraison']['statut'], 'Assigné livreur')

    def test_livraison_tracking_endpoint(self):
        client = Client.objects.create(nom='Faly', telephone='0346677889', adresse='Tana')
        commande = Commande.objects.create(client=client, produit=self.produit, variante=self.variante, ordre_jp=1)
        livraison = Livraison.objects.create(commande=commande, statut=Livraison.STATUT_PREPARATION, localisation_actuelle='Bureau', tracking_notes='Colis en cours de préparation')

        response = self.client.get('/api/livraisons/tracking/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['localisation_actuelle'], 'Bureau')

        response = self.client.get('/api/livraisons/tracking/', {'commande_id': commande.id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['commande_id'], commande.id)


class BackendGapsAPITest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='vendeur_test', password='password123')
        self.vendeur = Vendeur.objects.create(user=self.user, nom='Vendeur Chic', contact='0341112223')
        self.produit = create_test_produit(
            self.vendeur,
            nom='Robe Noire',
            variante={
                'taille': 'L',
                'couleur': 'Noir',
                'prix_unitaire': '60000.00',
                'stock': 5,
                'code_jp': 'JPNOIR',
            },
        )
        self.variante = self.produit.variantes.first()

    def test_stock_lifecycle_on_confirmation(self):
        client = Client.objects.create(nom='Sahondra', telephone='0345556667', adresse='Tana')
        commande = Commande.objects.create(
            client=client, produit=self.produit, variante=self.variante, statut=Commande.STATUT_JP_CAPTURE
        )

        self.assertEqual(self.variante.stock, 5)

        commande.statut = Commande.STATUT_CONFIRME
        commande.save()

        self.variante.refresh_from_db()
        self.assertEqual(self.variante.stock, 4)

        commande.statut = Commande.STATUT_ANNULE
        commande.save()

        self.variante.refresh_from_db()
        self.assertEqual(self.variante.stock, 5)

    def test_facebook_webhook_capture(self):
        payload = {
            'sender_facebook_id': 'fb_12345',
            'sender_name': 'Rabe',
            'comment_text': 'JP Robe Noire'
        }
        response = self.client.post('/api/webhooks/facebook/', payload, content_type='application/json')
        self.assertEqual(response.status_code, 201)

        client = Client.objects.get(facebook_id='fb_12345')
        self.assertEqual(client.nom, 'Rabe')
        self.assertEqual(Commande.objects.filter(client=client).count(), 1)

    def test_tiktok_webhook_capture(self):
        payload = {
            'sender_tiktok_id': 'tt_67890',
            'sender_name': 'Koto',
            'comment_text': 'JP Robe Noire'
        }
        response = self.client.post('/api/webhooks/tiktok/', payload, content_type='application/json')
        self.assertEqual(response.status_code, 201)

        client = Client.objects.get(tiktok_id='tt_67890')
        self.assertEqual(client.nom, 'Koto')

    def test_upload_payment_screenshot(self):
        client = Client.objects.create(nom='Aina', telephone='0341234567', adresse='Tana')
        commande = Commande.objects.create(client=client, produit=self.produit, variante=self.variante)

        mock_file = SimpleUploadedFile("receipt.png", b"file_content", content_type="image/png")

        response = self.client.post(
            f'/api/commandes/{commande.id}/upload-paiement/',
            {'file': mock_file},
            format='multipart'
        )
        self.assertEqual(response.status_code, 200)

        commande.refresh_from_db()
        self.assertEqual(commande.statut, Commande.STATUT_CONFIRME)
        self.assertEqual(commande.paiement.statut, Paiement.STATUT_PAYE)
        self.assertEqual(commande.paiement.methode, Paiement.METHODE_MOBILE_MONEY)
        self.assertIn('receipt', commande.paiement.capture_mobile_money)
        self.assertTrue(commande.paiement.capture_mobile_money.endswith('.png'))

    def test_thermal_label_generation(self):
        client = Client.objects.create(nom='Fara', telephone='0339999999', adresse='Tana')
        commande = Commande.objects.create(client=client, produit=self.produit, variante=self.variante)

        response = self.client.get(f'/api/commandes/{commande.id}/etiquette-jp/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        # Le code est stocke nu ('NOIR') et affiche avec un seul prefixe 'JP' (pas 'JP JP').
        self.assertIn('JP NOIR ROBE NOIRE', data['label_text'])
        self.assertNotIn('JP JP', data['label_text'])
        self.assertIn('60,000 Ar', data['label_text'])

    def test_azexpress_shipping_dispatch(self):
        client = Client.objects.create(nom='Rina', telephone='0328888888', adresse='Tana')
        commande = Commande.objects.create(client=client, produit=self.produit, variante=self.variante)

        response = self.client.post(f'/api/commandes/{commande.id}/lancer-livraison/')
        self.assertEqual(response.status_code, 200)

        commande.refresh_from_db()
        self.assertEqual(commande.statut, Commande.STATUT_EN_LIVRAISON)
        self.assertEqual(commande.livraison.statut, Livraison.STATUT_EN_LIVRAISON)
        self.assertIn('AZX-', commande.livraison.tracking_notes)

    def test_double_ship_blocked(self):
        client = Client.objects.create(nom='Tovo', telephone='0321111111', adresse='Tana')
        commande = Commande.objects.create(client=client, produit=self.produit, variante=self.variante)

        r1 = self.client.post(f'/api/commandes/{commande.id}/lancer-livraison/')
        self.assertEqual(r1.status_code, 200)

        r2 = self.client.post(f'/api/commandes/{commande.id}/lancer-livraison/')
        self.assertEqual(r2.status_code, 409)
        self.assertIn('déjà en statut', r2.json()['detail'])

    def test_dashboard_statistics(self):
        client1 = Client.objects.create(nom='User 1', telephone='0341', adresse='A')
        Commande.objects.create(client=client1, produit=self.produit, variante=self.variante, statut=Commande.STATUT_CONFIRME)

        client2 = Client.objects.create(nom='User 2', telephone='0342', adresse='B')
        Commande.objects.create(client=client2, produit=self.produit, variante=self.variante, statut=Commande.STATUT_JP_CAPTURE)

        response = self.client.get('/api/dashboard/stats/', {'vendeur_id': self.vendeur.id})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['nombre_jps'], 2)
        self.assertEqual(data['confirmes'], 1)
        self.assertEqual(data['chiffre_affaires'], 60000.00)
        self.assertEqual(data['montant_a_reverser'], 54000.00)

    def test_dashboard_requires_vendeur_id(self):
        response = self.client.get('/api/dashboard/stats/')
        self.assertEqual(response.status_code, 403)

    def test_client_serializer_exposes_social_ids(self):
        payload = {
            'sender_facebook_id': 'fb_audit_test',
            'sender_name': 'Audit User',
            'comment_text': 'JP Robe Noire'
        }
        response = self.client.post('/api/webhooks/facebook/', payload, content_type='application/json')
        self.assertEqual(response.status_code, 201)
        commande_data = response.json()['results'][0]['commande']
        self.assertIn('facebook_id', commande_data['client'])
        self.assertEqual(commande_data['client']['facebook_id'], 'fb_audit_test')

    def test_social_connect_disconnect_endpoints(self):
        self.assertFalse(self.vendeur.is_demo_mode)
        self.assertIsNone(self.vendeur.facebook_page_id)

        payload = {'vendeur_id': self.vendeur.id, 'platform': 'facebook'}
        response = self.client.post('/api/vendeurs/connect/', payload, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['facebook_page_id'], 'fb_page_123456789')
        self.assertEqual(response.json()['facebook_page_name'], 'Ma Boutique Facebook Officielle')

        payload = {'vendeur_id': self.vendeur.id, 'platform': 'demo'}
        response = self.client.post('/api/vendeurs/connect/', payload, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['is_demo_mode'])
        self.assertIsNone(response.json()['facebook_page_id'])

        payload = {'vendeur_id': self.vendeur.id, 'platform': 'all'}
        response = self.client.post('/api/vendeurs/disconnect/', payload, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['is_demo_mode'])

    def test_live_session_endpoints(self):
        from .models import Live, Collaborateur
        collab = Collaborateur.objects.create(nom='Clare Michel', role='operateur', vendeur=self.vendeur)
        live = Live.objects.create(titre="Dressing d'Hiver Premium Antsirabe", vendeur=self.vendeur, operateur=collab)

        response = self.client.get('/api/lives/')
        self.assertEqual(response.status_code, 200)
        results = response.json()['results']
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['titre'], "Dressing d'Hiver Premium Antsirabe")
        self.assertEqual(results[0]['operateur_nom'], 'Clare Michel')

    def test_product_variants_endpoints(self):
        variant = Variante.objects.create(
            produit=self.produit,
            taille='M',
            couleur='Noir',
            stock=2,
            prix_unitaire='60000.00',
            code_jp='JPNOIR2',
        )

        response = self.client.get(f'/api/produits/{self.produit.id}/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data['variantes']), 2)
        self.assertEqual(data['variantes'][0]['taille'], self.variante.taille)
        # Le code est normalise (nu, sans prefixe 'JP') a l'enregistrement.
        self.assertEqual(data['variantes'][1]['code_jp'], 'NOIR2')

    def test_client_stats_and_fidelity_endpoints(self):
        client = Client.objects.create(nom='Faratiana Rabe', telephone='0342255588', social_handle='@fara_rabe')

        response = self.client.get('/api/clients/')
        self.assertEqual(response.status_code, 200)
        results = response.json()['results']
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['social_handle'], '@fara_rabe')
        self.assertEqual(results[0]['sessions_count'], 0)

        Commande.objects.create(client=client, produit=self.produit, variante=self.variante, statut=Commande.STATUT_CONFIRME)
        Commande.objects.create(client=client, produit=self.produit, variante=self.variante, statut=Commande.STATUT_CONFIRME)

        response = self.client.get('/api/clients/stats/', {'vendeur_id': self.vendeur.id})
        self.assertEqual(response.status_code, 200)
        stats = response.json()
        self.assertEqual(stats['nombre_clients'], 1)
        self.assertEqual(stats['clients_fideles_count'], 1)
        self.assertEqual(stats['taux_fidelite'], 100.0)

    def test_facebook_pages_list(self):
        payload = {'vendeur_id': self.vendeur.id, 'platform': 'facebook'}
        self.client.post('/api/vendeurs/connect/', payload, content_type='application/json')

        response = self.client.get('/api/vendeurs/facebook-pages/', {'vendeur_id': self.vendeur.id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 4)
        self.assertEqual(response.json()[0]['nom'], 'AZLive Fashion')

    def test_live_dressing_association(self):
        from .models import Live
        live = Live.objects.create(titre="Live test dressing", vendeur=self.vendeur)

        payload = {
            'produits_dressing_ids': [self.produit.id],
            'pages_facebook': ['AZLive Fashion', 'Boutique Chic Madagascar']
        }
        response = self.client.patch(f'/api/lives/{live.id}/', payload, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['produits_dressing']), 1)
        self.assertEqual(response.json()['produits_dressing'][0]['id'], self.produit.id)
        self.assertEqual(response.json()['pages_facebook'], ['AZLive Fashion', 'Boutique Chic Madagascar'])

    def test_malagasy_queue_promotion_logic(self):
        client_a = Client.objects.create(nom='Aina', telephone='0341122334')
        client_b = Client.objects.create(nom='Bodo', telephone='0321122334')
        client_c = Client.objects.create(nom='Chantal', telephone='0334455667')

        response_a = self.client.post('/api/jp-capture/', {
            'comment_text': f"JP {self.produit.nom}",
            'telephone': client_a.telephone,
            'nom': client_a.nom
        }, content_type='application/json')
        self.assertEqual(response_a.status_code, 201)
        self.assertEqual(response_a.json()['commande']['ordre_jp'], 1)
        self.assertIn("nahazo ny JP-nao amin'ny", response_a.json()['message_envoye'])

        response_b = self.client.post('/api/jp-capture/', {
            'comment_text': f"JP {self.produit.nom}",
            'telephone': client_b.telephone,
            'nom': client_b.nom
        }, content_type='application/json')
        self.assertEqual(response_b.status_code, 201)
        self.assertEqual(response_b.json()['commande']['ordre_jp'], 2)
        self.assertIn("lisitra miandry", response_b.json()['message_envoye'])

        response_c = self.client.post('/api/jp-capture/', {
            'comment_text': f"JP {self.produit.nom}",
            'telephone': client_c.telephone,
            'nom': client_c.nom
        }, content_type='application/json')
        self.assertEqual(response_c.status_code, 201)
        self.assertEqual(response_c.json()['commande']['ordre_jp'], 3)

        cmd_a = Commande.objects.get(id=response_a.json()['commande']['id'])
        cmd_a.statut = Commande.STATUT_ANNULE
        cmd_a.save()

        cmd_b = Commande.objects.get(id=response_b.json()['commande']['id'])
        cmd_b.delete()


class FacebookOAuthAPITest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='vendeur_fb', password='password123')
        self.vendeur = Vendeur.objects.create(user=self.user, nom='Vendeur FB', contact='0340000000')

    def test_facebook_login_url_not_configured(self):
        response = self.client.get('/api/auth/facebook/login/')
        self.assertEqual(response.status_code, 503)

    @staticmethod
    def _override_facebook_settings():
        return override_settings(
            FACEBOOK_APP_ID='test-app-id',
            FACEBOOK_APP_SECRET='test-app-secret',
            FACEBOOK_REDIRECT_URI='http://localhost:8000/api/auth/facebook/callback/',
            FACEBOOK_LOGIN_SUCCESS_URL='http://localhost:3000/auth/facebook/success',
        )

    def test_facebook_login_url_configured(self):
        with self._override_facebook_settings():
            response = self.client.get('/api/auth/facebook/login/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('auth_url', response.json())
        self.assertIn('state', response.json())
        self.assertIn('facebook.com', response.json()['auth_url'])

    def test_facebook_token_login_creates_vendeur(self):
        with self._override_facebook_settings():
            with self.mock_facebook_api():
                response = self.client.post(
                    '/api/auth/facebook/token/',
                    {'access_token': 'short-lived-token'},
                    content_type='application/json',
                )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['created'])
        self.assertIn('token', data)
        self.assertEqual(data['vendeur']['nom'], 'Marie Rakoto')
        self.assertEqual(data['user']['email'], 'marie@example.com')

    def test_facebook_token_login_links_existing_vendeur(self):
        self.vendeur.facebook_user_id = 'fb-user-1'
        self.vendeur.save()

        with self._override_facebook_settings():
            with self.mock_facebook_api():
                response = self.client.post(
                    '/api/auth/facebook/token/',
                    {'access_token': 'short-lived-token'},
                    content_type='application/json',
                )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data['created'])
        self.assertEqual(data['vendeur']['id'], self.vendeur.id)

    def test_facebook_sync_pages(self):
        self.vendeur.facebook_user_id = 'fb-user-1'
        self.vendeur.facebook_access_token = 'long-lived-token'
        self.vendeur.save()
        token, _ = Token.objects.get_or_create(user=self.user)

        with self._override_facebook_settings():
            with self.mock_facebook_pages():
                response = self.client.post(
                    '/api/auth/facebook/sync-pages/',
                    content_type='application/json',
                    HTTP_AUTHORIZATION=f'Token {token.key}',
                )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['pages_synced'], 2)
        self.vendeur.refresh_from_db()
        self.assertEqual(self.vendeur.facebook_page_name, 'AZLive Fashion')

    def test_social_connect_requires_facebook_login_when_configured(self):
        with self._override_facebook_settings():
            payload = {'vendeur_id': self.vendeur.id, 'platform': 'facebook'}
            response = self.client.post('/api/vendeurs/connect/', payload, content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('Connectez-vous d\'abord via Facebook', response.json()['detail'])

    def test_social_connect_syncs_real_pages_when_token_present(self):
        self.vendeur.facebook_access_token = 'long-lived-token'
        self.vendeur.save()

        with self._override_facebook_settings():
            with self.mock_facebook_pages():
                payload = {'vendeur_id': self.vendeur.id, 'platform': 'facebook'}
                response = self.client.post('/api/vendeurs/connect/', payload, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['facebook_page_name'], 'AZLive Fashion')
        self.assertEqual(len(response.json()['pages_facebook']), 2)

    def test_auth_me_endpoint(self):
        token, _ = Token.objects.get_or_create(user=self.user)
        response = self.client.get('/api/auth/me/', HTTP_AUTHORIZATION=f'Token {token.key}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['vendeur']['id'], self.vendeur.id)
        self.assertFalse(response.json()['facebook_connected'])

    @staticmethod
    def mock_facebook_api():
        return patch.multiple(
            'backend.facebook_oauth',
            get_user_profile=lambda access_token: {
                'id': 'fb-user-1',
                'name': 'Marie Rakoto',
                'email': 'marie@example.com',
            },
            exchange_for_long_lived_token=lambda token: 'long-lived-token',
        )

    @staticmethod
    def mock_facebook_pages():
        return patch(
            'backend.facebook_oauth.get_user_pages',
            lambda access_token: [
                {'id': '123456789', 'name': 'AZLive Fashion', 'access_token': 'page-token-1'},
                {'id': '987654321', 'name': 'Boutique Chic', 'access_token': 'page-token-2'},
            ],
        )


class FacebookWebhookMetaTest(TestCase):
    def setUp(self):
        self.vendeur = Vendeur.objects.create(nom='Vendeur Live', contact='0341234567')
        self.produit = create_test_produit(
            self.vendeur,
            nom='Robe Noire',
            variante={
                'taille': 'M',
                'couleur': 'Noir',
                'prix_unitaire': '45000.00',
                'stock': 10,
                'code_jp': 'JPNOIR',
            },
        )
        self.page = PageFacebook.objects.create(
            vendeur=self.vendeur,
            page_id='123456789',
            nom='AZLive Fashion',
            access_token='page-token-1',
        )
        self.live = Live.objects.create(
            titre='Live Robe Noire',
            vendeur=self.vendeur,
            statut=Live.STATUT_EN_COURS,
            pages_facebook=['AZLive Fashion'],
        )
        self.live.produits_dressing.add(self.produit)

    @staticmethod
    def _meta_payload(message='JP Robe Noire', sender_id='fb_meta_001', sender_name='Meta User'):
        return {
            'object': 'page',
            'entry': [
                {
                    'id': '123456789',
                    'time': 1710000000,
                    'changes': [
                        {
                            'field': 'feed',
                            'value': {
                                'item': 'comment',
                                'verb': 'add',
                                'comment_id': 'comment_1',
                                'post_id': '123456789_987654321',
                                'message': message,
                                'from': {'id': sender_id, 'name': sender_name},
                            },
                        }
                    ],
                }
            ],
        }

    def test_meta_webhook_payload_capture(self):
        response = self.client.post(
            '/api/webhooks/facebook/',
            self._meta_payload(),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data['processed'], 1)
        self.assertEqual(data['results'][0]['status'], 'JP capturé avec succès')
        self.assertEqual(data['results'][0]['live_id'], self.live.id)
        self.assertEqual(data['results'][0]['vendeur_id'], self.vendeur.id)

        client = Client.objects.get(facebook_id='fb_meta_001')
        self.assertEqual(client.nom, 'Meta User')
        commande = Commande.objects.get(client=client)
        self.assertEqual(commande.live_id, self.live.id)

    def test_meta_webhook_ignores_non_purchase_comment(self):
        response = self.client.post(
            '/api/webhooks/facebook/',
            self._meta_payload(message='Bonjour tout le monde'),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['results'][0]['status'], 'ignored')

    @override_settings(FACEBOOK_APP_ID='test-app-id', FACEBOOK_APP_SECRET='test-app-secret')
    def test_meta_webhook_rejects_invalid_signature(self):
        response = self.client.post(
            '/api/webhooks/facebook/',
            self._meta_payload(),
            content_type='application/json',
            HTTP_X_HUB_SIGNATURE_256='sha256=invalid',
        )
        self.assertEqual(response.status_code, 403)

    def test_subscribe_webhooks_endpoint(self):
        user = User.objects.create_user(username='vendeur_wh', password='password123')
        self.vendeur.user = user
        self.vendeur.save()
        token, _ = Token.objects.get_or_create(user=user)

        with FacebookOAuthAPITest._override_facebook_settings():
            with patch(
                'backend.facebook_webhooks.subscribe_page_webhooks',
                lambda page_id, page_access_token: {'success': True},
            ):
                response = self.client.post(
                    '/api/auth/facebook/subscribe-webhooks/',
                    content_type='application/json',
                    HTTP_AUTHORIZATION=f'Token {token.key}',
                )

        self.assertEqual(response.status_code, 200)
        self.page.refresh_from_db()
        self.assertTrue(self.page.webhook_subscribed)
        self.assertEqual(len(response.json()['subscribed_pages']), 1)


class LiveDiffusionTest(TestCase):
    def setUp(self):
        self.vendeur = Vendeur.objects.create(
            nom='Vendeur Live',
            contact='0341234567',
            is_demo_mode=True,
            tiktok_username='@maboutique_tiktok',
        )
        self.page = PageFacebook.objects.create(
            vendeur=self.vendeur,
            page_id='123456789',
            nom='AZLive Fashion',
            access_token='page-token-1',
        )
        self.live = Live.objects.create(
            titre='Live Robe Noire',
            vendeur=self.vendeur,
            pages_facebook=['AZLive Fashion'],
        )

    def test_demarrer_live_starts_all_platforms(self):
        response = self.client.post(f'/api/lives/{self.live.id}/demarrer/')
        self.assertEqual(response.status_code, 200)
        data = response.json()['live']
        self.assertEqual(data['statut'], Live.STATUT_EN_COURS)
        self.assertEqual(len(data['diffusion_plateformes']['facebook']), 1)
        self.assertEqual(data['diffusion_plateformes']['tiktok']['status'], 'LIVE')
        self.assertIsNotNone(data['date_debut'])

    def test_arreter_live_stops_all_platforms(self):
        self.client.post(f'/api/lives/{self.live.id}/demarrer/')
        response = self.client.post(f'/api/lives/{self.live.id}/arreter/')
        self.assertEqual(response.status_code, 200)
        data = response.json()['live']
        self.assertEqual(data['statut'], Live.STATUT_TERMINE)
        self.assertEqual(data['diffusion_plateformes']['facebook'][0]['status'], 'ENDED')
        self.assertEqual(data['diffusion_plateformes']['tiktok']['status'], 'ENDED')
        self.assertIsNotNone(data['date_fin'])

    def test_starting_new_live_auto_stops_previous(self):
        live_b = Live.objects.create(titre='Live B', vendeur=self.vendeur, pages_facebook=['AZLive Fashion'])
        self.client.post(f'/api/lives/{self.live.id}/demarrer/')
        response = self.client.post(f'/api/lives/{live_b.id}/demarrer/')
        self.assertEqual(response.status_code, 200)

        self.live.refresh_from_db()
        live_b.refresh_from_db()
        self.assertEqual(self.live.statut, Live.STATUT_TERMINE)
        self.assertEqual(live_b.statut, Live.STATUT_EN_COURS)

    def test_patch_statut_triggers_start_and_stop(self):
        response = self.client.patch(
            f'/api/lives/{self.live.id}/',
            {'statut': Live.STATUT_EN_COURS},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['statut'], Live.STATUT_EN_COURS)
        self.assertTrue(response.json()['diffusion_plateformes'])

        response = self.client.patch(
            f'/api/lives/{self.live.id}/',
            {'statut': Live.STATUT_TERMINE},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['statut'], Live.STATUT_TERMINE)

    @staticmethod
    def _override_facebook_settings():
        return override_settings(
            FACEBOOK_APP_ID='test-app-id',
            FACEBOOK_APP_SECRET='test-app-secret',
        )

    def test_demarrer_live_real_facebook_mocked(self):
        self.vendeur.is_demo_mode = False
        self.vendeur.save()

        with self._override_facebook_settings():
            with patch(
                'backend.facebook_live.create_facebook_live_broadcast',
                lambda page, title, description='': {
                    'page_id': page.page_id,
                    'page_name': page.nom,
                    'live_video_id': 'fb_live_1',
                    'status': 'LIVE',
                    'stream_url': 'rtmp://live.facebook.com/app/stream',
                },
            ):
                response = self.client.post(f'/api/lives/{self.live.id}/demarrer/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()['live']['diffusion_plateformes']['facebook'][0]['live_video_id'],
            'fb_live_1',
        )


class TikTokOAuthAPITest(TestCase):
    @staticmethod
    def _override_tiktok_settings():
        return override_settings(
            TIKTOK_CLIENT_KEY='sb-test-key',
            TIKTOK_CLIENT_SECRET='sb-test-secret',
            TIKTOK_REDIRECT_URI='https://limacine-adrian-sighted.ngrok-free.dev/api/auth/tiktok/callback/',
            TIKTOK_LOGIN_SUCCESS_URL='http://localhost:3000/auth/tiktok/success',
        )

    def test_tiktok_login_url_not_configured(self):
        response = self.client.get('/api/auth/tiktok/login/')
        self.assertEqual(response.status_code, 503)

    def test_tiktok_login_url_configured(self):
        with self._override_tiktok_settings():
            response = self.client.get('/api/auth/tiktok/login/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('auth_url', response.json())
        self.assertIn('tiktok.com', response.json()['auth_url'])
        self.assertIn('code_challenge=', response.json()['auth_url'])
        self.assertIn('code_challenge_method=S256', response.json()['auth_url'])

    def test_tiktok_callback_creates_vendeur(self):
        with self._override_tiktok_settings():
            with patch('backend.tiktok_oauth.exchange_code_for_tokens') as mock_tokens:
                with patch('backend.tiktok_oauth.get_user_profile') as mock_profile:
                    mock_tokens.return_value = {
                        'access_token': 'tt-access',
                        'refresh_token': 'tt-refresh',
                        'open_id': 'open-123',
                    }
                    mock_profile.return_value = {
                        'open_id': 'open-123',
                        'display_name': 'Vendeur TikTok',
                        'username': 'vendeur_tt',
                    }
                    login = self.client.get('/api/auth/tiktok/login/')
                    state = login.json()['state']
                    response = self.client.get(
                        '/api/auth/tiktok/callback/',
                        {'code': 'auth-code', 'state': state, 'format': 'json'},
                    )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['created'])
        self.assertIn('token', data)
        self.assertEqual(data['vendeur']['tiktok_username'], '@vendeur_tt')


class TikToolLiveTest(TestCase):
    @staticmethod
    def _override_tiktool_settings():
        return override_settings(TIKTOOL_API_KEY='tk_test_key')

    def setUp(self):
        self.vendeur = Vendeur.objects.create(
            nom='Vendeur TT',
            contact='0341234567',
            tiktok_username='@maboutique',
        )
        self.produit = create_test_produit(self.vendeur, nom='Robe Noire', variante={
            'taille': 'M',
            'couleur': 'Noire',
            'prix_unitaire': '45000.00',
            'stock': 10,
            'code_jp': 'JP-ROBE-N',
        })
        self.live = Live.objects.create(
            titre='Live TikTok',
            vendeur=self.vendeur,
            statut=Live.STATUT_EN_COURS,
        )

    def test_resolve_vendeur_from_tiktok_username(self):
        vendeur = resolve_vendeur_from_tiktok_username('maboutique')
        self.assertEqual(vendeur, self.vendeur)

    def test_process_tiktool_chat_event(self):
        result = process_tiktool_chat_event(
            'maboutique',
            {
                'comment': 'JP Robe Noire',
                'user': {'uniqueId': 'viewer_1', 'nickname': 'Koto'},
            },
        )
        self.assertEqual(result['status'], 'JP capturé avec succès')
        self.assertEqual(result['live_id'], self.live.id)
        self.assertEqual(result['vendeur_id'], self.vendeur.id)

    def test_tiktool_webhook_payload(self):
        with self._override_tiktool_settings():
            response = self.client.post(
                '/api/webhooks/tiktok/',
                {
                    'event': 'chat',
                    'uniqueId': 'maboutique',
                    'data': {
                        'comment': 'JP Robe Noire',
                        'user': {'uniqueId': 'viewer_2', 'nickname': 'Aina'},
                    },
                },
                content_type='application/json',
            )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Client.objects.filter(tiktok_id='viewer_2').count(), 1)

    def test_demarrer_live_starts_tiktool_listener(self):
        self.vendeur.is_demo_mode = False
        self.vendeur.save()
        live = Live.objects.create(
            titre='Live prod',
            vendeur=self.vendeur,
            pages_facebook=[],
        )
        with self._override_tiktool_settings():
            with patch('backend.live_service.start_tiktool_listener', return_value=True) as mock_start:
                with patch('backend.live_service.build_tiktok_diffusion') as mock_diffusion:
                    mock_diffusion.return_value = {
                        'username': '@maboutique',
                        'unique_id': 'maboutique',
                        'status': 'PENDING_MANUAL',
                        'demo': False,
                    }
                    response = self.client.post(f'/api/lives/{live.id}/demarrer/')
        self.assertEqual(response.status_code, 200)
        mock_start.assert_called_once()


class OrderConfirmationFlowTest(TestCase):
    def setUp(self):
        self.vendeur = Vendeur.objects.create(nom='Vendeur', contact='0341234567')
        self.produit = create_test_produit(self.vendeur, nom='Robe Noire', variante={
            'taille': 'M', 'couleur': 'Noire', 'prix_unitaire': '45000.00', 'stock': 10, 'code_jp': 'JPNOIR',
        })
        self.client_obj = Client.objects.create(
            nom='Client TikTok',
            telephone='',
            adresse='',
            tiktok_id='viewer_confirm_1',
        )
        self.live = Live.objects.create(titre='Live', vendeur=self.vendeur, statut=Live.STATUT_EN_COURS)
        self.commande = Commande.objects.create(
            client=self.client_obj,
            produit=self.produit,
            variante=self.produit.variantes.first(),
            live=self.live,
            ordre_jp=1,
        )

    def test_parse_confirmation_text(self):
        from .order_confirmation import parse_confirmation_text

        parsed = parse_confirmation_text(
            'Nom : Rabe\nFinday : 0341122334\nAdiresy : Antananarivo Ankadifotsy\nDaty : 20/06/2026\nOra : 14h'
        )
        self.assertEqual(parsed['nom'], 'Rabe')
        self.assertIn('0341122334', parsed['telephone'])
        self.assertEqual(parsed['heure_livraison'], '14h')

    def test_parse_freeform_malagasy_lines(self):
        from .order_confirmation import parse_confirmation_text

        parsed = parse_confirmation_text('Lova\nBypass\n12 mai\n14h')
        self.assertEqual(parsed['nom'], 'Lova')
        self.assertEqual(parsed['adresse'], 'Bypass')
        self.assertEqual(parsed['date_livraison'], '12 mai')
        self.assertEqual(parsed['heure_livraison'], '14h')

    def test_freeform_without_phone_or_time_is_incomplete(self):
        response = self.client.post(
            f'/api/commandes/{self.commande.id}/confirmer/',
            {'message_text': 'Lova\nBypass\n12 mai', 'channel': 'TikTok'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data['complet'])
        self.assertIn('telephone', data['champs_manquants'])
        self.assertIn('heure_livraison', data['champs_manquants'])
        self.assertEqual(data['client']['nom'], 'Lova')
        self.assertEqual(data['client']['adresse'], 'Bypass')
        self.commande.refresh_from_db()
        self.assertEqual(self.commande.statut, Commande.STATUT_JP_CAPTURE)
        self.client_obj.refresh_from_db()
        self.assertEqual(self.client_obj.nom, 'Lova')

    def test_incremental_messages_complete_order(self):
        steps = [
            'Lova',
            '0341122334',
            'Bypass',
            '12 mai',
            '14h',
            '2',  # quantité demandée pendant la collecte
        ]
        for index, text in enumerate(steps):
            response = self.client.post(
                f'/api/commandes/{self.commande.id}/confirmer/',
                {'message_text': text, 'channel': 'TikTok'},
                content_type='application/json',
            )
            self.assertEqual(response.status_code, 200)
            if index < len(steps) - 1:
                self.assertFalse(response.json()['complet'])
            else:
                self.assertTrue(response.json()['complet'])
        self.commande.refresh_from_db()
        self.assertEqual(self.commande.statut, Commande.STATUT_CONFIRME)

    @override_settings(AZLIVE_PUBLIC_BASE_URL='http://testserver')
    def test_confirm_commande_via_api(self):
        response = self.client.post(
            f'/api/commandes/{self.commande.id}/confirmer/',
            {
                'message_text': 'Lova\n0341122334\nBypass\n12 mai\n14h\n2',
                'channel': 'TikTok',
            },
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['complet'])
        self.assertEqual(response.json()['status'], 'Commande confirmée')
        self.commande.refresh_from_db()
        self.assertEqual(self.commande.statut, Commande.STATUT_CONFIRME)
        self.assertIn('facture.pdf', response.json()['facture_url'])

    def test_facture_pdf_endpoint(self):
        self.commande.statut = Commande.STATUT_CONFIRME
        self.commande.save()
        self.client_obj.telephone = '0341122334'
        self.client_obj.adresse = 'Tana'
        self.client_obj.save()
        response = self.client.get(f'/api/commandes/{self.commande.id}/facture.pdf')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertTrue(response.content.startswith(b'%PDF'))

    def test_etiquette_livraison_pdf_endpoint(self):
        response = self.client.get(f'/api/commandes/{self.commande.id}/etiquette-livraison.pdf')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')

    def test_facebook_messaging_webhook_confirms_order(self):
        self.client_obj.facebook_id = 'fb_confirm_user'
        self.client_obj.tiktok_id = None
        self.client_obj.save()
        payload = {
            'object': 'page',
            'entry': [{
                'id': '123456789',
                'messaging': [{
                    'sender': {'id': 'fb_confirm_user'},
                    'recipient': {'id': '123456789'},
                    'timestamp': 1234567890,
                    'message': {
                        'mid': 'mid.1',
                        'text': 'Nom : Marie\nFinday : 0349988776\nAdiresy : Toamasina\nDaty : 25/06/2026\nOra : 15h30\nQuantité : 1',
                    },
                }],
            }],
        }
        PageFacebook.objects.create(
            vendeur=self.vendeur,
            page_id='123456789',
            nom='Page Test',
            access_token='page-token',
        )
        with patch('backend.order_messaging.send_facebook_private_message', return_value={'sent': True}):
            response = self.client.post(
                '/api/webhooks/facebook/',
                payload,
                content_type='application/json',
            )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()['results'][0].get('complet'))
        self.commande.refresh_from_db()
        self.assertEqual(self.commande.statut, Commande.STATUT_CONFIRME)


class QuantiteEtAnnulationTest(TestCase):
    def setUp(self):
        self.vendeur = Vendeur.objects.create(nom='Vendeur', contact='0341234567')
        self.produit = create_test_produit(self.vendeur, nom='Robe Noire', variante={
            'taille': 'M', 'couleur': 'Noire', 'prix_unitaire': '45000.00', 'stock': 10, 'code_jp': 'JPNOIR',
        })
        self.variante = self.produit.variantes.first()

    def _capture(self, comment_text, sender_id='tt_qty', sender_name='Acheteur'):
        from .jp_capture import process_social_comment

        return process_social_comment(
            sender_id=sender_id,
            sender_name=sender_name,
            comment_text=comment_text,
            channel='TikTok',
            vendeur=self.vendeur,
            id_field='tiktok_id',
        )

    def _confirm(self, commande_id, message_text):
        return self.client.post(
            f'/api/commandes/{commande_id}/confirmer/',
            {'message_text': message_text, 'channel': 'TikTok'},
            content_type='application/json',
        )

    def test_quantite_pas_lue_dans_le_commentaire(self):
        # Même si le commentaire contient un nombre, on ne fixe PAS la quantité au JP.
        result = self._capture('JP Robe Noire 3')
        commande = Commande.objects.get(id=result['commande']['id'])
        self.assertIsNone(commande.quantite)

    def test_quantite_demandee_pendant_la_collecte(self):
        result = self._capture('JP Robe Noire')
        commande_id = result['commande']['id']
        # Tant que la quantité n'est pas donnée, la commande n'est pas confirmée.
        partial = self._confirm(commande_id, 'Lova\n0341122334\nBypass\n12 mai\n14h')
        self.assertEqual(partial.status_code, 200)
        self.assertFalse(partial.json()['complet'])
        self.assertIn('quantite', partial.json()['champs_manquants'])
        # Le client répond la quantité → confirmation.
        final = self._confirm(commande_id, '3')
        self.assertTrue(final.json()['complet'])
        commande = Commande.objects.get(id=commande_id)
        self.assertEqual(commande.quantite, 3)
        self.assertEqual(commande.statut, Commande.STATUT_CONFIRME)

    def test_doublon_meme_article_reutilise_la_commande(self):
        first = self._capture('JP Robe Noire')
        second = self._capture('JP Robe Noire')
        self.assertEqual(first['commande']['id'], second['commande']['id'])
        self.assertEqual(Commande.objects.filter(client__tiktok_id='tt_qty').count(), 1)

    def test_stock_decremente_selon_la_quantite(self):
        result = self._capture('JP Robe Noire')
        commande_id = result['commande']['id']
        response = self._confirm(commande_id, 'Lova\n0341122334\nBypass\n12 mai\n14h\n3')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['complet'])
        self.variante.refresh_from_db()
        self.assertEqual(self.variante.stock, 7)

    def test_annulation_apres_confirmation_restaure_le_stock(self):
        result = self._capture('JP Robe Noire')
        commande_id = result['commande']['id']
        self._confirm(commande_id, 'Lova\n0341122334\nBypass\n12 mai\n14h\n2')
        self.variante.refresh_from_db()
        self.assertEqual(self.variante.stock, 8)

        response = self._confirm(commande_id, 'Annuler')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json().get('annule'))
        commande = Commande.objects.get(id=commande_id)
        self.assertEqual(commande.statut, Commande.STATUT_ANNULE)
        self.variante.refresh_from_db()
        self.assertEqual(self.variante.stock, 10)

    def test_annulation_en_malgache(self):
        result = self._capture('JP Robe Noire')
        commande_id = result['commande']['id']
        self._confirm(commande_id, 'Lova\n0341122334\nBypass\n12 mai\n14h\n1')
        self.assertEqual(Commande.objects.get(id=commande_id).statut, Commande.STATUT_CONFIRME)

        response = self._confirm(commande_id, 'Foano ny baiko')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json().get('annule'))
        self.assertEqual(Commande.objects.get(id=commande_id).statut, Commande.STATUT_ANNULE)

    def test_infos_en_desordre_completent_la_commande(self):
        # Nom placeholder (aucun vrai nom fourni par la plateforme) → le client doit le donner.
        result = self._capture('JP Robe Noire', sender_name='Client TikTok')
        commande_id = result['commande']['id']
        # Ordre volontairement mélangé : téléphone, nom, adresse, date, heure, quantité.
        for text in ('0341122334', 'Lova', 'Bypass', '12 mai', '14h', '2'):
            response = self._confirm(commande_id, text)
            self.assertEqual(response.status_code, 200)
        commande = Commande.objects.get(id=commande_id)
        self.assertEqual(commande.statut, Commande.STATUT_CONFIRME)
        self.assertEqual(commande.client.nom, 'Lova')
        self.assertEqual(commande.client.telephone, '0341122334')
        self.assertEqual(commande.quantite, 2)

    def test_client_en_liste_attente_non_confirme_meme_avec_infos(self):
        self.variante.stock = 1
        self.variante.save()
        a = self._capture('JP Robe Noire', sender_id='ttA', sender_name='Client TikTok')
        b = self._capture('JP Robe Noire', sender_id='ttB', sender_name='Client TikTok')
        cmd_a = Commande.objects.get(id=a['commande']['id'])
        cmd_b = Commande.objects.get(id=b['commande']['id'])
        self.assertEqual(cmd_a.ordre_jp, 1)
        self.assertEqual(cmd_b.ordre_jp, 2)

        # B (en attente) envoie pourtant ses infos complètes → reste en attente, sans stock.
        resp_b = self._confirm(cmd_b.id, 'Bodo\n0331122334\nBypass\n12 mai\n14h\n1')
        self.assertEqual(resp_b.status_code, 200)
        self.assertTrue(resp_b.json().get('en_attente'))
        cmd_b.refresh_from_db()
        self.assertEqual(cmd_b.statut, Commande.STATUT_JP_CAPTURE)
        self.variante.refresh_from_db()
        self.assertEqual(self.variante.stock, 1)

        # A (en tête) confirme normalement.
        resp_a = self._confirm(cmd_a.id, 'Aina\n0320000000\nLot\n12 mai\n14h\n1')
        self.assertTrue(resp_a.json()['complet'])
        self.variante.refresh_from_db()
        self.assertEqual(self.variante.stock, 0)
        cmd_b.refresh_from_db()
        self.assertEqual(cmd_b.statut, Commande.STATUT_JP_CAPTURE)

    def test_promotion_auto_confirme_le_suivant_complet(self):
        self.variante.stock = 1
        self.variante.save()
        a = self._capture('JP Robe Noire', sender_id='ttA', sender_name='Client TikTok')
        b = self._capture('JP Robe Noire', sender_id='ttB', sender_name='Client TikTok')
        cmd_a = Commande.objects.get(id=a['commande']['id'])
        cmd_b = Commande.objects.get(id=b['commande']['id'])

        self._confirm(cmd_b.id, 'Bodo\n0331122334\nBypass\n12 mai\n14h\n1')  # en attente, infos complètes
        self._confirm(cmd_a.id, 'Aina\n0320000000\nLot\n12 mai\n14h\n1')      # confirmé, stock 0
        self.variante.refresh_from_db()
        self.assertEqual(self.variante.stock, 0)

        # A annule → la place se libère → B (complet) est confirmé automatiquement.
        self._confirm(cmd_a.id, 'Annuler')
        cmd_b.refresh_from_db()
        self.assertEqual(cmd_b.statut, Commande.STATUT_CONFIRME)
        self.variante.refresh_from_db()
        self.assertEqual(self.variante.stock, 0)
        # Message dédié : commande prise en compte car une place s'est libérée.
        last_b = cmd_b.messages.filter(direction=Message.DIRECTION_OUTBOUND).order_by('-date_envoi').first()
        self.assertIn('toerana malalaka', last_b.contenu)
        self.assertIn('voafahana', last_b.contenu)

    def test_promotion_incomplet_demande_les_infos(self):
        self.variante.stock = 1
        self.variante.save()
        a = self._capture('JP Robe Noire', sender_id='ttA', sender_name='Client TikTok')
        b = self._capture('JP Robe Noire', sender_id='ttB', sender_name='Client TikTok')
        cmd_a = Commande.objects.get(id=a['commande']['id'])
        cmd_b = Commande.objects.get(id=b['commande']['id'])

        # B (en attente) n'envoie qu'une partie de ses infos.
        self._confirm(cmd_b.id, 'Bodo')
        cmd_b.refresh_from_db()
        self.assertEqual(cmd_b.statut, Commande.STATUT_JP_CAPTURE)

        # A confirme puis annule → B est promu mais incomplet → on lui demande ses infos.
        self._confirm(cmd_a.id, 'Aina\n0320000000\nLot\n12 mai\n14h\n1')
        self._confirm(cmd_a.id, 'Annuler')

        cmd_b.refresh_from_db()
        self.assertEqual(cmd_b.statut, Commande.STATUT_JP_CAPTURE)  # toujours en attente d'infos
        last_b = cmd_b.messages.filter(direction=Message.DIRECTION_OUTBOUND).order_by('-date_envoi').first()
        self.assertIn('toerana malalaka', last_b.contenu)
        self.assertIn('alefaso', last_b.contenu)  # on demande d'envoyer les infos

    def test_expiration_par_delai_fait_monter_le_suivant(self):
        from django.core.management import call_command

        self.variante.stock = 1
        self.variante.save()
        a = self._capture('JP Robe Noire', sender_id='ttA', sender_name='Client TikTok')
        b = self._capture('JP Robe Noire', sender_id='ttB', sender_name='Client TikTok')
        cmd_a = Commande.objects.get(id=a['commande']['id'])
        cmd_b = Commande.objects.get(id=b['commande']['id'])

        # B (en attente) a déjà tout fourni.
        self._confirm(cmd_b.id, 'Bodo\n0331122334\nBypass\n12 mai\n14h\n1')
        cmd_b.refresh_from_db()
        self.assertEqual(cmd_b.statut, Commande.STATUT_JP_CAPTURE)

        # A (en tête) ne confirme jamais : 3 relances puis expiration (--force ignore le délai).
        for _ in range(4):
            call_command('run_relances', '--force')

        cmd_a.refresh_from_db()
        cmd_b.refresh_from_db()
        self.assertEqual(cmd_a.statut, Commande.STATUT_ANNULE)        # A expiré
        self.assertEqual(cmd_b.statut, Commande.STATUT_CONFIRME)      # B promu et confirmé
        self.variante.refresh_from_db()
        self.assertEqual(self.variante.stock, 0)

    def test_client_en_attente_pas_de_relance(self):
        from django.core.management import call_command

        self.variante.stock = 1
        self.variante.save()
        self._capture('JP Robe Noire', sender_id='ttA', sender_name='Client TikTok')
        b = self._capture('JP Robe Noire', sender_id='ttB', sender_name='Client TikTok')
        cmd_b = Commande.objects.get(id=b['commande']['id'])
        relances_avant = cmd_b.messages.count()

        call_command('run_relances', '--force')

        # B est en liste d'attente : il ne reçoit pas de relance.
        self.assertEqual(cmd_b.messages.count(), relances_avant)

    def test_file_attente_3_personnes_selon_stock_et_quantites(self):
        # Stock 5 : P1 prend 4, P2 prend le reste (1), P3 attend la file.
        self.variante.stock = 5
        self.variante.save()
        a = self._capture('JP Robe Noire', sender_id='p1', sender_name='Client TikTok')
        b = self._capture('JP Robe Noire', sender_id='p2', sender_name='Client TikTok')
        c = self._capture('JP Robe Noire', sender_id='p3', sender_name='Client TikTok')
        cmd_a = Commande.objects.get(id=a['commande']['id'])
        cmd_b = Commande.objects.get(id=b['commande']['id'])
        cmd_c = Commande.objects.get(id=c['commande']['id'])

        # P1 confirme 4 -> reste 1.
        r1 = self._confirm(cmd_a.id, 'Aina\n0320000001\nLot\n12 mai\n14h\n4')
        self.assertTrue(r1.json()['complet'])
        self.variante.refresh_from_db()
        self.assertEqual(self.variante.stock, 1)

        # P2 confirme 1 (le reste) -> reste 0.
        r2 = self._confirm(cmd_b.id, 'Bodo\n0320000002\nBypass\n12 mai\n14h\n1')
        self.assertTrue(r2.json()['complet'])
        self.variante.refresh_from_db()
        self.assertEqual(self.variante.stock, 0)

        # P3 fournit tout mais le stock est épuisé -> liste d'attente.
        r3 = self._confirm(cmd_c.id, 'Cara\n0320000003\nIvato\n12 mai\n14h\n1')
        self.assertTrue(r3.json().get('en_attente'))
        cmd_c.refresh_from_db()
        self.assertEqual(cmd_c.statut, Commande.STATUT_JP_CAPTURE)

        # P2 annule -> une place se libère -> P3 (complet) confirmé automatiquement.
        self._confirm(cmd_b.id, 'Annuler')
        cmd_c.refresh_from_db()
        self.assertEqual(cmd_c.statut, Commande.STATUT_CONFIRME)
        self.variante.refresh_from_db()
        self.assertEqual(self.variante.stock, 0)

    def test_jp_keyword_insensible_a_la_casse(self):
        # "jp", "JP", "Jp" déclenchent tous la capture.
        for txt, sid in (('jp Robe Noire', 'lc1'), ('Jp Robe Noire', 'lc2'), ('jP Robe Noire', 'lc3')):
            res = self._capture(txt, sender_id=sid, sender_name='Client TikTok')
            self.assertEqual(res['status'], 'JP capturé avec succès')


class JPCodeNormalizationTest(TestCase):
    def test_normalize_strips_jp_prefix(self):
        from .jp_codes import format_jp_code, normalize_jp_code

        self.assertEqual(normalize_jp_code('JP NOIR'), 'NOIR')
        self.assertEqual(normalize_jp_code('JPNOIR'), 'NOIR')
        self.assertEqual(normalize_jp_code(' jp-001 '), '001')
        self.assertEqual(normalize_jp_code('NOIR'), 'NOIR')
        self.assertEqual(normalize_jp_code(None), '')

    def test_format_never_doubles_jp(self):
        from .jp_codes import format_jp_code

        self.assertEqual(format_jp_code('JPNOIR'), 'JP NOIR')
        self.assertEqual(format_jp_code('NOIR'), 'JP NOIR')
        self.assertNotIn('JP JP', format_jp_code('JP JPNOIR'))

    def test_variante_save_stores_bare_code(self):
        vendeur = Vendeur.objects.create(nom='Vendeur', contact='0341234567')
        produit = create_test_produit(vendeur, nom='Robe', variante={
            'taille': 'M', 'couleur': 'Noir', 'prix_unitaire': '1000.00', 'stock': 5, 'code_jp': 'JPNOIR',
        })
        variante = produit.variantes.first()
        self.assertEqual(variante.code_jp, 'NOIR')


class LiveCodeResolutionTest(TestCase):
    def setUp(self):
        from .models import Live

        self.vendeur = Vendeur.objects.create(nom='Vendeur', contact='0341234567')
        self.produit_a = create_test_produit(self.vendeur, nom='Robe Rouge', variante={
            'taille': 'M', 'couleur': 'Rouge', 'prix_unitaire': '45000.00', 'stock': 10, 'code_jp': 'A',
        })
        self.produit_b = create_test_produit(self.vendeur, nom='Robe Bleue', variante={
            'taille': 'M', 'couleur': 'Bleu', 'prix_unitaire': '50000.00', 'stock': 10, 'code_jp': 'B',
        })
        self.var_a = self.produit_a.variantes.first()
        self.var_b = self.produit_b.variantes.first()
        self.live1 = Live.objects.create(titre='Live 1', vendeur=self.vendeur, statut=Live.STATUT_EN_COURS)
        self.live2 = Live.objects.create(titre='Live 2', vendeur=self.vendeur, statut=Live.STATUT_EN_COURS)

    def _capture(self, comment_text, live, sender_id):
        from .jp_capture import process_social_comment

        return process_social_comment(
            sender_id=sender_id,
            sender_name='Acheteur',
            comment_text=comment_text,
            channel='TikTok',
            vendeur=self.vendeur,
            live=live,
            id_field='tiktok_id',
        )

    def test_same_code_reusable_across_lives_without_collision(self):
        from .models import LiveCodeJP

        # Le code "1" pointe vers des produits differents selon le live.
        LiveCodeJP.objects.create(live=self.live1, variante=self.var_a, code='1')
        LiveCodeJP.objects.create(live=self.live2, variante=self.var_b, code='1')

        self.assertEqual(LiveCodeJP.objects.filter(code='1').count(), 2)

    def test_resolution_uses_live_specific_mapping(self):
        from .models import Commande, LiveCodeJP

        LiveCodeJP.objects.create(live=self.live1, variante=self.var_a, code='1')
        LiveCodeJP.objects.create(live=self.live2, variante=self.var_b, code='1')

        res1 = self._capture('JP 1', self.live1, sender_id='ttLive1')
        cmd1 = Commande.objects.get(id=res1['commande']['id'])
        self.assertEqual(cmd1.variante_id, self.var_a.id)
        self.assertEqual(cmd1.produit_id, self.produit_a.id)

        res2 = self._capture('JP 1', self.live2, sender_id='ttLive2')
        cmd2 = Commande.objects.get(id=res2['commande']['id'])
        self.assertEqual(cmd2.variante_id, self.var_b.id)
        self.assertEqual(cmd2.produit_id, self.produit_b.id)

    def test_double_jp_legacy_still_resolves(self):
        from .models import Commande, LiveCodeJP

        LiveCodeJP.objects.create(live=self.live1, variante=self.var_a, code='NOIR')
        # Ancien comportement : un client tape par erreur "JP JPNOIR" -> doit resoudre vers NOIR.
        res = self._capture('JP JPNOIR', self.live1, sender_id='ttLegacy')
        cmd = Commande.objects.get(id=res['commande']['id'])
        self.assertEqual(cmd.variante_id, self.var_a.id)

    def test_code_resolution_case_insensitive(self):
        from .models import Commande, LiveCodeJP

        LiveCodeJP.objects.create(live=self.live1, variante=self.var_a, code='A')
        for txt, sid in (('jp a', 'ci1'), ('JP A', 'ci2'), ('Jp a', 'ci3')):
            res = self._capture(txt, self.live1, sender_id=sid)
            cmd = Commande.objects.get(id=res['commande']['id'])
            self.assertEqual(cmd.variante_id, self.var_a.id)


class LiveCodesEndpointTest(TestCase):
    def setUp(self):
        from .models import Live

        self.vendeur = Vendeur.objects.create(nom='Vendeur', contact='0341234567')
        self.produit_a = create_test_produit(self.vendeur, nom='Robe Rouge', variante={
            'taille': 'M', 'couleur': 'Rouge', 'prix_unitaire': '45000.00', 'stock': 10, 'code_jp': 'A',
        })
        self.produit_b = create_test_produit(self.vendeur, nom='Robe Bleue', variante={
            'taille': 'M', 'couleur': 'Bleu', 'prix_unitaire': '50000.00', 'stock': 10, 'code_jp': 'B',
        })
        self.var_a = self.produit_a.variantes.first()
        self.var_b = self.produit_b.variantes.first()
        self.live = Live.objects.create(titre='Live', vendeur=self.vendeur)

    def _post_codes(self, live_id, codes):
        return self.client.post(
            f'/api/lives/{live_id}/codes/',
            {'codes': codes},
            content_type='application/json',
        )

    def test_upsert_codes(self):
        resp = self._post_codes(self.live.id, [
            {'variante_id': self.var_a.id, 'code': 'JP 1'},
            {'variante_id': self.var_b.id, 'code': '2'},
        ])
        self.assertEqual(resp.status_code, 200)
        from .models import LiveCodeJP

        self.assertEqual(LiveCodeJP.objects.get(live=self.live, variante=self.var_a).code, '1')
        self.assertEqual(LiveCodeJP.objects.get(live=self.live, variante=self.var_b).code, '2')

    def test_duplicate_code_in_same_live_rejected(self):
        self._post_codes(self.live.id, [{'variante_id': self.var_a.id, 'code': '1'}])
        resp = self._post_codes(self.live.id, [{'variante_id': self.var_b.id, 'code': '1'}])
        self.assertEqual(resp.status_code, 409)

    def test_duplicate_code_case_insensitive_rejected(self):
        # 'ABC' et 'abc' sont le meme code (normalises en majuscules).
        self._post_codes(self.live.id, [{'variante_id': self.var_a.id, 'code': 'ABC'}])
        resp = self._post_codes(self.live.id, [{'variante_id': self.var_b.id, 'code': 'abc'}])
        self.assertEqual(resp.status_code, 409)

    def test_same_code_other_live_ok(self):
        from .models import Live

        other = Live.objects.create(titre='Autre', vendeur=self.vendeur)
        r1 = self._post_codes(self.live.id, [{'variante_id': self.var_a.id, 'code': '1'}])
        r2 = self._post_codes(other.id, [{'variante_id': self.var_b.id, 'code': '1'}])
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)

    def test_empty_code_removes_mapping(self):
        from .models import LiveCodeJP

        self._post_codes(self.live.id, [{'variante_id': self.var_a.id, 'code': '1'}])
        self.assertTrue(LiveCodeJP.objects.filter(live=self.live, variante=self.var_a).exists())
        self._post_codes(self.live.id, [{'variante_id': self.var_a.id, 'code': ''}])
        self.assertFalse(LiveCodeJP.objects.filter(live=self.live, variante=self.var_a).exists())

