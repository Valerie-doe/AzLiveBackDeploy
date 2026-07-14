import logging
import uuid
from django.utils import timezone

from .message_humanizer import emoji, greeting, pick

logger = logging.getLogger(__name__)


class MessagingService:
    @staticmethod
    def send_automatic_message(client, produit, order_id) -> bool:
        """
        Simulates sending the initial JP message via WhatsApp/Messenger in Malagasy.
        """
        demande = pick([
            "Mba alefaso anay azafady ny anaranao, numéro, adresse ary ny daty hanaterana.",
            "Mba hahavita ny commande, omeo anay ny anaranao, numéro, adresse ary ny daty hanaterana.",
        ])
        message_content = (
            f"{greeting(client.nom)} 😊 Voaray ny JP-nao ho an'ny '{produit.nom}'. {demande}{emoji(prob=0.3)}"
        )
        logger.info(f"[SMS/MESSENGER MOCK] Envoyé à {client.telephone or 'Client ID ' + str(client.id)} (Commande #{order_id}) : '{message_content}'")
        print(f"\n [MESSAGING SERVICE] Message envoyé avec succès à {client.nom} ({client.telephone or 'Social Platform'}):")
        print(f"   > '{message_content}'\n")
        return True

    @staticmethod
    def send_relance_message(client, produit, numero_relance) -> bool:
        """
        Simulates sending a follow-up relance message via Messenger in Malagasy.
        """
        message_content = (
            f"{greeting(client.nom)}! Fampahatsiahivana kely momba ny commande-nao '{produit.nom}'. "
            f"Mbola miandry ny adresse-nao sy ny daty hanaterana izahay azafady.{emoji(prob=0.3)}"
        )
        logger.info(f"[SMS/MESSENGER RELANCE MOCK] Relance #{numero_relance} envoyée à {client.telephone or 'Client ID ' + str(client.id)} : '{message_content}'")
        print(f"\n⏰ [MESSAGING SERVICE] Relance #{numero_relance} envoyée à {client.nom} ({client.telephone or 'Social Platform'}):")
        print(f"   > '{message_content}'\n")
        return True

    @staticmethod
    def send_waiting_list_message(client, produit, ordre_jp, order_id) -> bool:
        """
        Simulates sending a waiting list notification in Malagasy.
        """
        message_content = (
            f"{greeting(client.nom)} 😊 Voaray ny JP-nao ho an'ny '{produit.nom}'. "
            f"Fa efa misy nanao commande mialoha, ka ao amin'ny liste d'attente ianao izao (numéro {ordre_jp}). "
            f"Hilazanay anao raha vao misy toerana."
        )
        logger.info(f"[SMS/MESSENGER WAITING MOCK] Envoyé à {client.telephone or 'Client ID ' + str(client.id)} (Commande #{order_id}) : '{message_content}'")
        print(f"\n [MESSAGING SERVICE] Message de liste d'attente envoyé à {client.nom} ({client.telephone or 'Social Platform'}):")
        print(f"   > '{message_content}'\n")
        return True

    @staticmethod
    def send_promotion_message(client, produit, order_id) -> bool:
        """
        Simulates sending a promotion notification in Malagasy.
        """
        message_content = (
            f"{greeting(client.nom)}, vaovao tsara! Anjaranao izao ho an'ny '{produit.nom}'. "
            f"Mba alefaso anay azafady ny anaranao, numéro, adresse ary ny daty hanaterana.{emoji(prob=0.3)}"
        )
        logger.info(f"[SMS/MESSENGER PROMOTION MOCK] Envoyé à {client.telephone or 'Client ID ' + str(client.id)} (Commande #{order_id}) : '{message_content}'")
        print(f"\n [MESSAGING SERVICE] Message de promotion envoyé à {client.nom} ({client.telephone or 'Social Platform'}):")
        print(f"   > '{message_content}'\n")
        return True


class AZExpressService:
    @staticmethod
    def transmettre_colis(commande, livraison) -> dict:
        """
        Simulates transmitting package information to AZExpress shipping API.
        Returns mock tracking number and success payload.
        """
        tracking_number = f"AZX-{uuid.uuid4().hex[:8].upper()}"
        
        # Log the payload that would be sent to AZExpress API
        variante = commande.variante or commande.produit.variantes.order_by('id').first()
        variante_label = (
            f"{commande.produit.nom} ({variante.couleur}, {variante.taille})"
            if variante else commande.produit.nom
        )
        montant = float(variante.prix_unitaire) * commande.quantite_effective if variante else 0

        payload = {
            "commande_id": commande.id,
            "vendeur": commande.produit.vendeur.nom,
            "client_nom": commande.client.nom,
            "client_telephone": commande.client.telephone,
            "client_adresse": commande.client.adresse,
            "produit": variante_label,
            "quantite": commande.quantite_effective,
            "montant_a_percevoir": montant,
            "tracking_number": tracking_number
        }
        
        logger.info(f"[AZEXPRESS API MOCK] Colis transmis pour Commande #{commande.id}. Payload : {payload}")
        print(f"\n [AZEXPRESS SERVICE] Synchronisation réussie pour Commande #{commande.id} :")
        print(f"   > Code Tracking AZExpress généré : {tracking_number}")
        print(f"   > Livreur assigné par défaut : {livraison.livreur.nom if livraison.livreur else 'Aucun'}\n")
        
        return {
            "status": "success",
            "tracking_number": tracking_number,
            "assigned_carrier": "AZExpress Dispatcher",
            "estimated_delivery": (timezone.now() + timezone.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        }
