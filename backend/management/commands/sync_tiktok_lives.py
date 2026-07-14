from django.core.management.base import BaseCommand

from backend.tiktool_live import sync_external_tiktok_lives, tiktool_configured


class Command(BaseCommand):
    help = (
        "Détecte les lives TikTok via WebSocket (roomInfo) puis 1× room_id si besoin. "
        "À lancer pendant qu'un live TikTok est réellement en cours."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--no-rest',
            action='store_true',
            help='Ne pas appeler l’API REST (WebSocket uniquement).',
        )
        parser.add_argument(
            '--wait',
            type=float,
            default=20.0,
            help='Secondes d’attente du signal WebSocket (défaut: 20).',
        )

    def handle(self, *args, **options):
        if not tiktool_configured():
            self.stdout.write(self.style.WARNING('TIKTOOL_API_KEY manquant : sync ignorée.'))
            return

        result = sync_external_tiktok_lives(
            min_interval_seconds=0,
            rest=not options['no_rest'],
            wait_ws_seconds=float(options['wait']),
        )
        if result.get('ws_rate_limited'):
            self.stdout.write(
                self.style.ERROR(
                    'Quota WebSocket sandbox épuisé (60 connexions/heure, code 4429). '
                    'Attends jusqu’à 1h sans relancer sync/runserver en boucle, '
                    'sinon chaque reconnexion brûle encore le quota.'
                )
            )
        if result.get('rate_limited'):
            self.stdout.write(
                self.style.WARNING(
                    'TikTools rate-limité API (429) : réessaie quand remaining > 0 '
                    '(python manage.py tiktool_quota).'
                )
            )
            return
        if result.get('throttled'):
            self.stdout.write(self.style.WARNING('Sync ignorée (throttle).'))
            return

        self.stdout.write(
            self.style.SUCCESS(
                'Synchronisation terminée: '
                f"{result.get('started', 0)} live(s) détecté(s), "
                f"{result.get('stopped', 0)} live(s) clôturé(s), "
                f"{result.get('skipped', 0)} vendeur(s) sans preuve live."
            )
        )
        if result.get('started', 0) == 0 and not result.get('ws_rate_limited'):
            self.stdout.write(
                self.style.NOTICE(
                    'Astuce: 1) quota API remaining>0 et WS non 4429  2) live TikTok ON  '
                    '3) `python manage.py runserver` (1 scout stable, sans reconnect loop).'
                )
            )
