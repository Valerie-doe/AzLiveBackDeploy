from django.core.management.base import BaseCommand, CommandError

from backend.models import Live
from backend.tiktool_live import listener_status, start_tiktool_listener, stop_tiktool_listener, tiktool_configured


class Command(BaseCommand):
    help = 'Démarre ou arrête l\'écoute TikTools des commentaires live TikTok pour un live actif.'

    def add_arguments(self, parser):
        parser.add_argument('--live-id', type=int, help='ID du live AZLive à écouter')
        parser.add_argument('--stop', action='store_true', help='Arrête l\'écoute pour ce live')
        parser.add_argument('--status', action='store_true', help='Affiche l\'état du listener')

    def handle(self, *args, **options):
        if not tiktool_configured():
            raise CommandError('TIKTOOL_API_KEY manquant dans .env')

        live_id = options.get('live_id')
        if not live_id:
            raise CommandError('Précisez --live-id')

        live = Live.objects.select_related('vendeur').filter(pk=live_id).first()
        if not live:
            raise CommandError(f'Live #{live_id} introuvable')

        if options['status']:
            status = listener_status(live_id)
            self.stdout.write(str(status))
            return

        if options['stop']:
            stopped = stop_tiktool_listener(live)
            if stopped:
                self.stdout.write(self.style.SUCCESS(f'Listener TikTools arrêté pour live #{live_id}'))
            else:
                self.stdout.write(self.style.WARNING(f'Aucun listener actif pour live #{live_id}'))
            return

        if live.statut != Live.STATUT_EN_COURS:
            raise CommandError(
                f'Le live #{live_id} n\'est pas en cours. Démarrez-le via POST /api/lives/{live_id}/demarrer/'
            )

        if not live.vendeur.tiktok_username:
            raise CommandError('Ce vendeur n\'a pas de tiktok_username configuré.')

        started = start_tiktool_listener(live)
        if started:
            self.stdout.write(
                self.style.SUCCESS(
                    f'Listener TikTools démarré pour live #{live_id} (@{live.vendeur.tiktok_username.lstrip("@")})'
                )
            )
            self.stdout.write(
                'Lancez le live sur TikTok, puis postez un commentaire "JP ..." pour tester la capture.'
            )
        else:
            raise CommandError('Impossible de démarrer le listener TikTools.')
