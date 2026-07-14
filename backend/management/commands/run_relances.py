from django.core.management.base import BaseCommand

from backend.jp_relances import process_jp_relances


class Command(BaseCommand):
    help = (
        "Relance les clients en tête de file d'attente JP qui n'ont pas confirmé leurs "
        "informations, et libère leur place (expiration) après le nombre maximum de relances. "
        "Le suivant de la file monte alors automatiquement. "
        "(En prod / runserver, un planificateur auto tourne déjà — cette commande reste "
        "utile pour un passage manuel ou --force.)"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            help="Ignore le délai d'attente et traite immédiatement les commandes.",
        )

    def handle(self, *args, **options):
        force = options['force']
        self.stdout.write(self.style.NOTICE('Traitement des relances et expirations JP...'))

        result = process_jp_relances(force=force)

        if result.get('inbox_synced'):
            self.stdout.write(self.style.SUCCESS(
                f"Inbox Messenger : {result['inbox_synced']} message(s) traité(s)."
            ))

        for item in result.get('relances') or []:
            delivery = item.get('delivery') or {}
            self.stdout.write(self.style.SUCCESS(
                f"Relance #{item['numero_relance']} envoyée pour Commande #{item['commande_id']} "
                f"sent={delivery.get('sent')}"
            ))

        for commande_id in result.get('expirations') or []:
            self.stdout.write(self.style.WARNING(
                f'Commande #{commande_id} expirée (max relances atteint) — place libérée pour la file.'
            ))

        self.stdout.write(self.style.SUCCESS(
            f"Terminé. Relances : {result['relances_count']} | "
            f"Expirations : {result['expirations_count']}"
        ))
