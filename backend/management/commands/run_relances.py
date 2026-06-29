from datetime import timedelta
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Max
from django.utils import timezone

from backend.models import Commande, Message
from backend.order_confirmation import _order_is_eligible, expire_commande
from backend.services import MessagingService


class Command(BaseCommand):
    help = (
        "Relance les clients en tête de file d'attente JP qui n'ont pas confirmé leurs "
        "informations, et libère leur place (expiration) après le nombre maximum de relances. "
        "Le suivant de la file monte alors automatiquement."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            help="Ignore le délai d'attente et traite immédiatement les commandes.",
        )

    def handle(self, *args, **options):
        force = options['force']
        now = timezone.now()
        delay = getattr(settings, 'AZLIVE_JP_RELANCE_DELAY_MINUTES', 30)
        max_relances = getattr(settings, 'AZLIVE_JP_MAX_RELANCES', 3)

        relances_count = 0
        expirations_count = 0

        self.stdout.write(self.style.NOTICE("Traitement des relances et expirations JP..."))

        commandes = (
            Commande.objects.filter(statut=Commande.STATUT_JP_CAPTURE)
            .select_related('client', 'produit', 'variante')
        )

        for commande in commandes:
            # Une expiration précédente dans cette même passe a pu confirmer/avancer la file :
            # on relit le statut pour ne pas traiter une commande déjà sortie de la file.
            commande.refresh_from_db()
            if commande.statut != Commande.STATUT_JP_CAPTURE:
                continue

            # Seuls les clients ÉLIGIBLES (en tête, avec du stock pour eux) sont relancés :
            # ceux en liste d'attente ne peuvent pas encore confirmer, on ne les harcèle pas.
            if not _order_is_eligible(commande):
                continue

            last_message = commande.messages.order_by('-date_envoi').first()
            if not last_message:
                continue

            if not force and last_message.date_envoi + timedelta(minutes=delay) > now:
                continue

            relances_envoyees = commande.messages.aggregate(m=Max('numero_relance'))['m'] or 0

            if relances_envoyees < max_relances:
                relance_num = relances_envoyees + 1
                contenu = (
                    f"Salama {commande.client.nom}, fampatsiahivana faha-{relance_num} momba ny baikonao "
                    f"'{commande.produit.nom}'. Mba alefaso ny anarana, finday, adiresy, daty/ora ary ny isa "
                    f"mba hahafahanay manamafy azy."
                )
                Message.objects.create(commande=commande, contenu=contenu, numero_relance=relance_num)
                MessagingService.send_relance_message(commande.client, commande.produit, relance_num)
                relances_count += 1
                self.stdout.write(self.style.SUCCESS(
                    f"Relance #{relance_num} envoyée pour Commande #{commande.id} ({commande.client.nom})"
                ))
            else:
                expire_commande(commande)
                expirations_count += 1
                self.stdout.write(self.style.WARNING(
                    f"Commande #{commande.id} expirée (max relances atteint) — place libérée pour la file."
                ))

        self.stdout.write(self.style.SUCCESS(
            f"Terminé. Relances : {relances_count} | Expirations : {expirations_count}"
        ))
