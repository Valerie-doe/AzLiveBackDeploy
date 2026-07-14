"""Diagnostic de la capture JP (commentaires Facebook → commandes)."""

from django.core.management.base import BaseCommand

from backend.ai import HybridCommentAnalyzer
from backend.facebook_live_comments import (
    ensure_facebook_comment_listener,
    listener_status,
    recover_facebook_comment_listeners,
)
from backend.jp_capture import _candidate_code, resolve_live_variante
from backend.models import Commande, Live, LiveCodeJP


class Command(BaseCommand):
    help = 'Vérifie poller FB, codes JP du live et analyse de commentaires tests.'

    def add_arguments(self, parser):
        parser.add_argument('--live-id', type=int, help='ID du live (défaut : premier en cours)')
        parser.add_argument(
            '--recover',
            action='store_true',
            help='Redémarre le poller Facebook si absent ou mort',
        )
        parser.add_argument(
            '--comments',
            nargs='*',
            default=['2', 'JP2', 'jp 2', 'JE PRENDS JP2'],
            help='Commentaires tests à analyser',
        )

    def handle(self, *args, **options):
        live_id = options['live_id']
        if live_id:
            live = Live.objects.select_related('vendeur').filter(pk=live_id).first()
        else:
            live = (
                Live.objects.filter(statut=Live.STATUT_EN_COURS)
                .select_related('vendeur')
                .order_by('-date_live')
                .first()
            )

        if not live:
            self.stderr.write(self.style.ERROR('Aucun live trouvé.'))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(f'Live #{live.pk} — {live.statut}'))
        self.stdout.write(f'  Vendeur: {live.vendeur} (demo_mode={live.vendeur.is_demo_mode})')
        self.stdout.write(f'  Commandes: {Commande.objects.filter(live=live).count()}')

        fb = (live.diffusion_plateformes or {}).get('facebook') or []
        self.stdout.write(f'  Broadcasts Facebook: {len(fb)}')
        for b in fb:
            self.stdout.write(
                f"    - {b.get('page_name')} video={b.get('live_video_id')} status={b.get('status')}"
            )

        codes = LiveCodeJP.objects.filter(live=live).select_related('variante', 'variante__produit')
        self.stdout.write(f'  LiveCodeJP: {codes.count()} entrée(s)')
        for m in codes:
            self.stdout.write(
                f"    code={m.code!r} → {m.variante.produit.nom} "
                f"({m.variante.couleur}/{m.variante.taille})"
            )

        dressing = live.produits_dressing.prefetch_related('variantes').all()
        self.stdout.write(f'  Dressing: {dressing.count()} produit(s)')
        for p in dressing:
            for v in p.variantes.all():
                self.stdout.write(f"    {p.nom}: code_jp={v.code_jp!r} variante#{v.pk}")

        status = listener_status(live.pk)
        self.stdout.write(f'  Poller Facebook: {status}')

        if options['recover']:
            if ensure_facebook_comment_listener(live):
                self.stdout.write(self.style.SUCCESS('  Poller redémarré.'))
            else:
                n = recover_facebook_comment_listeners()
                self.stdout.write(self.style.WARNING(f'  recover_facebook_comment_listeners → {n}'))
            self.stdout.write(f'  Poller après recover: {listener_status(live.pk)}')

        analyzer = HybridCommentAnalyzer()
        self.stdout.write(self.style.MIGRATE_HEADING('Analyse de commentaires tests'))
        for text in options['comments']:
            analysis = analyzer.analyze(text, vendeur=live.vendeur, live=live)
            variante = resolve_live_variante(live, analysis, vendeur=live.vendeur)
            self.stdout.write(f'  {text!r}:')
            self.stdout.write(
                f"    intent={analysis.get('intent')} source={analysis.get('source')} "
                f"code_jp={analysis.get('code_jp')!r} product_query={analysis.get('product_query')!r}"
            )
            self.stdout.write(f'    _candidate_code={_candidate_code(analysis)!r}')
            if variante:
                self.stdout.write(
                    self.style.SUCCESS(
                        f'    -> variante #{variante.pk} {variante.produit.nom} code={variante.code_jp}'
                    )
                )
            else:
                self.stdout.write(self.style.WARNING('    -> variante NON resolue'))
