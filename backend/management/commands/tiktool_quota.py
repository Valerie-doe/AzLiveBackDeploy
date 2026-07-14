from django.conf import settings
from django.core.management.base import BaseCommand

import json
import urllib.error
import urllib.parse
import urllib.request


class Command(BaseCommand):
    help = "Affiche le quota TikTools (API + WebSocket sandbox)."

    def handle(self, *args, **options):
        api_key = getattr(settings, 'TIKTOOL_API_KEY', '') or ''
        if not api_key:
            self.stderr.write(self.style.ERROR('TIKTOOL_API_KEY manquant dans .env'))
            return

        url = 'https://api.tik.tools/webcast/rate_limits?' + urllib.parse.urlencode(
            {'apiKey': api_key}
        )
        try:
            with urllib.request.urlopen(url, timeout=15) as response:
                payload = json.loads(response.read().decode('utf-8', errors='replace'))
        except urllib.error.HTTPError as exc:
            self.stderr.write(self.style.ERROR(f'HTTP {exc.code}: {exc.reason}'))
            return
        except Exception as exc:  # noqa: BLE001
            self.stderr.write(self.style.ERROR(str(exc)))
            return

        data = payload.get('data') if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            self.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False))
            return

        api = data.get('api') or {}
        ws = data.get('websocket') or {}
        self.stdout.write(self.style.SUCCESS(f"Tier        : {data.get('tier')}"))
        self.stdout.write(
            f"API         : {api.get('remaining')}/{api.get('limit')} restantes "
            f"(reset_at={api.get('reset_at')})"
        )
        self.stdout.write(
            f"WebSocket   : {ws.get('current')}/{ws.get('limit')} connexions simultanées"
        )
        self.stdout.write(
            self.style.NOTICE(
                'Sandbox: aussi ~60 ouvertures WS / heure. Si close 4429, attendre ~1h '
                'sans relancer sync/scouts en boucle.'
            )
        )
        if 'bulk_check_limit' in data:
            self.stdout.write(f"Bulk check  : max {data.get('bulk_check_limit')}")
