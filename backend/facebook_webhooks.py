import hashlib
import hmac
import json
from typing import Any

from django.conf import settings

from .facebook_oauth import FacebookOAuthError, _graph_request
from .jp_capture import JPCaptureError, process_social_comment
from .order_confirmation import OrderConfirmationError, process_inbound_private_message


def verify_webhook_signature(raw_body: bytes, signature_header: str | None) -> bool:
    app_secret = settings.FACEBOOK_APP_SECRET
    if not app_secret:
        return True
    if not signature_header or not signature_header.startswith('sha256='):
        return False
    expected = hmac.new(
        app_secret.encode('utf-8'),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(f'sha256={expected}', signature_header)


def extract_facebook_comments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if payload.get('object') != 'page':
        return []

    comments = []
    subscribed_fields = [field.strip() for field in settings.FACEBOOK_WEBHOOK_FIELDS.split(',') if field.strip()]
    for entry in payload.get('entry', []):
        page_id = str(entry.get('id', ''))
        for change in entry.get('changes', []):
            field = change.get('field')
            if field not in subscribed_fields:
                continue

            value = change.get('value') or {}
            if value.get('item') != 'comment' or value.get('verb') != 'add':
                continue

            message = value.get('message') or value.get('text') or ''
            sender = value.get('from') or {}
            comments.append(
                {
                    'page_id': page_id,
                    'comment_id': value.get('comment_id'),
                    'post_id': value.get('post_id'),
                    'sender_facebook_id': str(sender.get('id', '')),
                    'sender_name': sender.get('name') or 'Client Facebook',
                    'comment_text': message,
                }
            )
    return comments


def extract_facebook_messaging_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if payload.get('object') != 'page':
        return []

    events = []
    for entry in payload.get('entry', []):
        page_id = str(entry.get('id', ''))
        for item in entry.get('messaging', []):
            message = item.get('message') or {}
            text = message.get('text') or ''
            if not text or message.get('is_echo'):
                continue
            sender_id = str((item.get('sender') or {}).get('id', ''))
            if not sender_id:
                continue
            events.append(
                {
                    'page_id': page_id,
                    'sender_facebook_id': sender_id,
                    'message_text': text,
                }
            )
    return events


def is_legacy_facebook_payload(payload: dict[str, Any]) -> bool:
    return bool(payload.get('sender_facebook_id') and payload.get('comment_text'))


def process_facebook_webhook_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if is_legacy_facebook_payload(payload):
        result = process_social_comment(
            sender_id=str(payload['sender_facebook_id']),
            sender_name=payload.get('sender_name', 'Client Facebook'),
            comment_text=payload['comment_text'],
            channel='Facebook',
            page_id=payload.get('page_id'),
            id_field='facebook_id',
        )
        status_code = 201 if result.get('status') != 'ignored' else 200
        return {'status_code': status_code, 'results': [result]}

    messaging_events = extract_facebook_messaging_events(payload)
    if messaging_events:
        results = []
        for event in messaging_events:
            try:
                result = process_inbound_private_message(
                    sender_id=event['sender_facebook_id'],
                    message_text=event['message_text'],
                    channel='Facebook',
                    page_id=event.get('page_id'),
                    id_field='facebook_id',
                )
                results.append(result)
            except OrderConfirmationError as exc:
                results.append({'status': 'error', 'detail': exc.message, **exc.payload})
        confirmed = any(r.get('status') == 'Commande confirmée' for r in results)
        return {'status_code': 201 if confirmed else 200, 'results': results}

    comments = extract_facebook_comments(payload)
    if not comments:
        return {
            'status_code': 200,
            'results': [{'status': 'ignored', 'detail': 'Aucun commentaire JP à traiter.'}],
        }

    results = []
    for comment in comments:
        if not comment.get('comment_text'):
            results.append({'status': 'ignored', 'detail': 'Commentaire vide.', 'comment_id': comment.get('comment_id')})
            continue
        try:
            result = process_social_comment(
                sender_id=comment['sender_facebook_id'],
                sender_name=comment['sender_name'],
                comment_text=comment['comment_text'],
                channel='Facebook',
                page_id=comment.get('page_id'),
                id_field='facebook_id',
                comment_id=comment.get('comment_id'),
            )
            results.append({**result, 'comment_id': comment.get('comment_id')})
        except JPCaptureError as exc:
            results.append(
                {
                    'status': 'error',
                    'detail': exc.message,
                    'comment_id': comment.get('comment_id'),
                    **exc.payload,
                }
            )

    captured = any(r.get('status') == 'JP capturé avec succès' for r in results)
    status_code = 201 if captured else 200
    return {'status_code': status_code, 'results': results}


def subscribe_page_webhooks(page_id: str, page_access_token: str) -> dict[str, Any]:
    subscribed_fields = settings.FACEBOOK_WEBHOOK_FIELDS
    return _graph_request(
        f'{page_id}/subscribed_apps',
        {
            'subscribed_fields': subscribed_fields,
            'access_token': page_access_token,
        },
        method='POST',
    )


def unsubscribe_page_webhooks(page_id: str, page_access_token: str) -> dict[str, Any]:
    return _graph_request(
        f'{page_id}/subscribed_apps',
        {'access_token': page_access_token},
        method='DELETE',
    )


def subscribe_vendeur_pages(vendeur) -> list[dict[str, Any]]:
    results = []
    pages = vendeur.pages_facebook.exclude(access_token__isnull=True).exclude(access_token='')
    if not pages.exists():
        raise FacebookOAuthError('Aucune page Facebook avec token disponible.', status_code=400)

    for page in pages:
        payload = subscribe_page_webhooks(page.page_id, page.access_token)
        page.webhook_subscribed = bool(payload.get('success'))
        page.save(update_fields=['webhook_subscribed'])
        results.append(
            {
                'page_id': page.page_id,
                'nom': page.nom,
                'success': page.webhook_subscribed,
            }
        )
    return results
