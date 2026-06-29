"""Démarre automatiquement les lives planifiés dont l'heure est arrivée.

À lancer périodiquement (cron toutes les minutes, ou Celery beat) :
    python manage.py start_scheduled_lives

Un live est démarré si :
  - son statut est 'planifie' ;
  - son heure planifiée (date_live) est <= maintenant ;
  - (option) son retard ne dépasse pas --max-delay-minutes.

Rappel : avec la diffusion par navigateur, le live Facebook est créé à l'heure dite,
mais aucune vidéo n'apparaît tant que le vendeur n'a pas ouvert sa caméra.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from backend.live_service import LiveServiceError, demarrer_live
from backend.models import Live


class Command(BaseCommand):
    help = "Démarre les lives planifiés dont l'heure de début est atteinte."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Affiche les lives qui seraient démarrés, sans les démarrer.',
        )
        parser.add_argument(
            '--max-delay-minutes',
            type=int,
            default=0,
            help=(
                'Ignore les lives en retard de plus de N minutes (évite de lancer '
                'un live planifié oublié). 0 = aucune limite (défaut).'
            ),
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        max_delay = options['max_delay_minutes']
        now = timezone.now()

        queryset = Live.objects.select_related('vendeur').filter(
            statut=Live.STATUT_PLANIFIE,
            date_live__lte=now,
        )
        if max_delay and max_delay > 0:
            floor = now - timedelta(minutes=max_delay)
            skipped = queryset.filter(date_live__lt=floor)
            for live in skipped:
                self.stdout.write(self.style.WARNING(
                    f"⏭  Live #{live.pk} «{live.titre}» ignoré (retard > {max_delay} min, "
                    f"planifié le {live.date_live:%Y-%m-%d %H:%M})."
                ))
            queryset = queryset.filter(date_live__gte=floor)

        queryset = queryset.order_by('date_live')

        if not queryset.exists():
            self.stdout.write(self.style.NOTICE("Aucun live planifié à démarrer pour le moment."))
            return

        started = 0
        for live in queryset:
            label = f"Live #{live.pk} «{live.titre}» (vendeur {live.vendeur.nom}, planifié {live.date_live:%H:%M})"
            if dry_run:
                self.stdout.write(self.style.NOTICE(f"[dry-run] démarrerait : {label}"))
                continue
            try:
                demarrer_live(live)
                started += 1
                self.stdout.write(self.style.SUCCESS(f"✔ Démarré : {label}"))
            except LiveServiceError as exc:
                self.stdout.write(self.style.ERROR(f"✗ Échec {label} : {exc.message}"))
            except Exception as exc:  # noqa: BLE001
                self.stdout.write(self.style.ERROR(f"✗ Erreur inattendue {label} : {exc}"))

        if not dry_run:
            self.stdout.write(self.style.SUCCESS(f"Terminé. Lives démarrés : {started}"))
