"""Diagnostic du Live Facebook + commentaires.

A lancer PENDANT qu'un live est actif et que la webcam diffuse :
    python diag_fb_comments.py

Il verifie, pour le live 'en_cours' :
  1. que l'objet live video existe et son statut Facebook (doit etre LIVE),
  2. que la page a bien un live video actif,
  3. les commentaires renvoyes par l'API (avec auteur 'from').
"""
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'AZLive.settings')
django.setup()

from backend.models import Live, PageFacebook
from backend.facebook_oauth import _graph_request, FacebookOAuthError


def safe_get(path, params):
    try:
        return _graph_request(path, params, method='GET')
    except FacebookOAuthError as exc:
        return {'__error__': str(exc), 'status': getattr(exc, 'status_code', None)}


live = Live.objects.filter(statut=Live.STATUT_EN_COURS).order_by('-date_live').first()
if not live:
    print(">>> AUCUN live 'en_cours'. Demarre un live ET garde-le actif, puis relance ce script.")
    raise SystemExit

print(f"LIVE #{live.pk} | demo={live.vendeur.is_demo_mode}")
fb = (live.diffusion_plateformes or {}).get('facebook') or []
target = next((b for b in fb if b.get('live_video_id') and b.get('page_id') and not b.get('demo')), None)
if not target:
    print(">>> Pas de broadcast Facebook reel exploitable (live_video_id manquant / demo).")
    raise SystemExit

lvid = target['live_video_id']
page = PageFacebook.objects.filter(page_id=str(target['page_id'])).first()
token = page.access_token
print(f"live_video_id = {lvid} | page = {page.page_id}")

# 1. Statut du live video
info = safe_get(lvid, {'access_token': token, 'fields': 'id,status,permalink_url'})
print("\n[1] STATUT live video:", info)
status = info.get('status') if isinstance(info, dict) else None
if status != 'LIVE':
    print("    >>> Le live n'est PAS en statut LIVE cote Facebook.")
    print("    >>> Cela signifie qu'aucune video n'arrive a Facebook (relais ffmpeg/MediaMTX).")
    print("    >>> Sans statut LIVE, personne ne peut commenter -> aucune capture possible.")

# 2. Live videos actifs de la page
lvs = safe_get(f"{page.page_id}/live_videos", {'access_token': token, 'fields': 'id,status', 'limit': 5})
print("\n[2] live_videos de la page:", lvs.get('data') if isinstance(lvs, dict) else lvs)

# 3. Commentaires
res = safe_get(f"{lvid}/comments", {
    'access_token': token,
    'fields': 'id,message,created_time,from{id,name}',
    'live_filter': 'no_filter',
    'order': 'reverse_chronological',
    'limit': 50,
})
data = res.get('data', []) if isinstance(res, dict) else []
print("\n[3] COMMENTAIRES:", len(data), "renvoye(s)")
if isinstance(res, dict) and res.get('__error__'):
    print("    ERREUR:", res['__error__'])
for c in data[:15]:
    frm = c.get('from') or {}
    print("   -", c.get('created_time'), "| from:", frm.get('id'), frm.get('name'), "| msg:", repr(c.get('message')))
