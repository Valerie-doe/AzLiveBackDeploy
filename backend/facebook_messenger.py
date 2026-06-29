import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

from .facebook_oauth import GRAPH_API_VERSION
from .models import PageFacebook

logger = logging.getLogger(__name__)


def send_facebook_private_message(page: PageFacebook, recipient_id: str, text: str) -> dict:
    if not page.access_token:
        return {'sent': False, 'error': 'Token page manquant.'}

    payload = {
        'recipient': json.dumps({'id': str(recipient_id)}),
        'message': json.dumps({'text': text}),
        'messaging_type': 'RESPONSE',
        'access_token': page.access_token,
    }
    data = urllib.parse.urlencode(payload).encode('utf-8')
    url = f'https://graph.facebook.com/{GRAPH_API_VERSION}/{page.page_id}/messages'
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            'Content-Type': 'application/x-www-form-urlencoded',
            'User-Agent': 'AZLive/1.0',
        },
        method='POST',
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = json.loads(response.read().decode('utf-8'))
        return {'sent': True, 'channel': 'Facebook', 'message_id': body.get('message_id')}
    except urllib.error.HTTPError as exc:
        try:
            error_payload = json.loads(exc.read().decode('utf-8'))
            message = error_payload.get('error', {}).get('message', str(error_payload))
        except (json.JSONDecodeError, UnicodeDecodeError):
            message = str(exc)
        logger.warning('Messenger send failed page %s: %s', page.page_id, message)
        return {'sent': False, 'error': message, 'channel': 'Facebook'}
    except urllib.error.URLError as exc:
        logger.warning('Messenger network error page %s: %s', page.page_id, exc.reason)
        return {'sent': False, 'error': str(exc.reason), 'channel': 'Facebook'}


def send_facebook_private_reply(page: PageFacebook, comment_id: str, text: str) -> dict:
    """Répond en privé à l'auteur d'un commentaire (live ou post).

    C'est le seul canal pour écrire à un commentateur de live : l'id renvoyé par l'API
    des commentaires n'est pas un PSID Messenger. On utilise l'endpoint Messenger
    /{page}/messages avec recipient.comment_id (méthode officielle ; l'ancien
    /{comment_id}/private_replies ne gère pas les commentaires de vidéo live).
    Nécessite la permission pages_messaging et, pour un live, un envoi pendant la
    diffusion. Renvoie aussi le recipient_id (PSID) pour les échanges ultérieurs.
    """
    if not page.access_token:
        return {'sent': False, 'error': 'Token page manquant.'}
    if not comment_id:
        return {'sent': False, 'error': 'comment_id manquant.'}

    payload = {
        'recipient': json.dumps({'comment_id': str(comment_id)}),
        'message': json.dumps({'text': text}),
        'messaging_type': 'RESPONSE',
        'access_token': page.access_token,
    }
    data = urllib.parse.urlencode(payload).encode('utf-8')
    url = f'https://graph.facebook.com/{GRAPH_API_VERSION}/{page.page_id}/messages'
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            'Content-Type': 'application/x-www-form-urlencoded',
            'User-Agent': 'AZLive/1.0',
        },
        method='POST',
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = json.loads(response.read().decode('utf-8'))
        return {
            'sent': True,
            'channel': 'Facebook',
            'via': 'private_reply',
            'message_id': body.get('message_id'),
            'recipient_id': body.get('recipient_id'),
        }
    except urllib.error.HTTPError as exc:
        try:
            error_payload = json.loads(exc.read().decode('utf-8'))
            message = error_payload.get('error', {}).get('message', str(error_payload))
        except (json.JSONDecodeError, UnicodeDecodeError):
            message = str(exc)
        logger.warning('Private reply failed comment %s: %s', comment_id, message)
        return {'sent': False, 'error': message, 'channel': 'Facebook', 'via': 'private_reply'}
    except urllib.error.URLError as exc:
        logger.warning('Private reply network error comment %s: %s', comment_id, exc.reason)
        return {'sent': False, 'error': str(exc.reason), 'channel': 'Facebook', 'via': 'private_reply'}
