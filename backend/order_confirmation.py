import json
import logging
import re
import unicodedata
from datetime import date, datetime, time, timedelta
from typing import Any

from django.db import models, transaction
from django.utils import timezone

from .models import Client, Commande, Live, Message, Paiement, PageFacebook, Vendeur
from .serializers import CommandeSerializer

logger = logging.getLogger(__name__)


class OrderConfirmationError(Exception):
    def __init__(self, message, status_code=400, payload=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload or {}


FIELD_PATTERNS = {
    'nom': re.compile(r'(?:^|\n)\s*(?:nom|anarana)\s*[:\-]\s*(.+)', re.IGNORECASE),
    'telephone': re.compile(r'(?:^|\n)\s*(?:tel(?:éphone)?|finday|phone)\s*[:\-]\s*(.+)', re.IGNORECASE),
    'adresse': re.compile(r'(?:^|\n)\s*(?:adres(?:se)?|adiresy)\s*[:\-]\s*(.+)', re.IGNORECASE),
    'date_livraison': re.compile(
        r'(?:^|\n)\s*(?:date(?:\s+livraison)?|daty)\s*[:\-]\s*(.+)',
        re.IGNORECASE,
    ),
    'heure_livraison': re.compile(
        r'(?:^|\n)\s*(?:heure|ora|time)\s*[:\-]\s*(.+)',
        re.IGNORECASE,
    ),
}

PHONE_PATTERN = re.compile(
    r'^(?:\+261[\s.-]?|0)(3[0-9]{2})[\s.-]?(\d{2})[\s.-]?(\d{3})[\s.-]?(\d{2})$'
)
PHONE_LOOSE_PATTERN = re.compile(r'(?:\+261|0)?3[0-9]{8}')

TIME_PATTERN = re.compile(
    r'^(\d{1,2})\s*[hH:]\s*(\d{2})?(?:\s*(?:min|ora))?$|^\d{1,2}:\d{2}$',
)

FRENCH_MONTHS = {
    'janvier': 1,
    'fevrier': 2,
    'mars': 3,
    'avril': 4,
    'mai': 5,
    'juin': 6,
    'juillet': 7,
    'aout': 8,
    'septembre': 9,
    'octobre': 10,
    'novembre': 11,
    'decembre': 12,
}

WEEKDAY_NAMES = {
    'lundi': 0,
    'alatsinainy': 0,
    'mardi': 1,
    'talata': 1,
    'mercredi': 2,
    'alarobia': 2,
    'jeudi': 3,
    'alakamisy': 3,
    'vendredi': 4,
    'zoma': 4,
    'samedi': 5,
    'sabotsy': 5,
    'dimanche': 6,
    'alahady': 6,
}

PERIOD_DEFAULT_TIMES = {
    'matin': time(9, 0),
    'maraina': time(9, 0),
    'aprem': time(14, 0),
    'apresmidi': time(14, 0),
    'atoandro': time(14, 0),
    'soir': time(18, 0),
    'hariva': time(18, 0),
}

WEEKDAY_PATTERN = re.compile(
    r'\b(' + '|'.join(sorted(WEEKDAY_NAMES.keys(), key=len, reverse=True)) + r')\b',
    re.IGNORECASE,
)
PERIOD_PATTERN = re.compile(
    r'\b(' + '|'.join(sorted(PERIOD_DEFAULT_TIMES.keys(), key=len, reverse=True)) + r')\b',
    re.IGNORECASE,
)
RELATIVE_HOUR_PERIOD_PATTERN = re.compile(
    r"(?:@|amin'?ny|ami'?ny|a|à)?\s*(\d{1,2})\s*h?\s*"
    r'(matin|maraina|atoandro|aprem|apres[\s\-]?midi|soir|hariva)\b',
    re.IGNORECASE,
)
CONNECTOR_PATTERN = re.compile(
    r"\b(?:amin'?ny|ami'?ny|ny|le|la|ce|cette|a|à)\b",
    re.IGNORECASE,
)
_GREETING_WORDS = (
    r'bonjour|salama(?:\s+e)?|manahoana|mana[o]?[\s\-]?ahoana|miarahaba(?:\s+anao)?|'
    r'bonsoir|coucou|hello|hi'
)
_GREETING_PARTICLES = r'e|ô|o|anao'
GREETING_PREFIX = re.compile(
    rf'^(?:(?:{_GREETING_WORDS}|{_GREETING_PARTICLES})\b[,\s!?.]*)+',
    re.IGNORECASE,
)
GREETING_TOKEN = re.compile(
    rf'^(?:{_GREETING_WORDS}|{_GREETING_PARTICLES})$',
    re.IGNORECASE,
)
CONFIRMATION_TYPO_FIXES = (
    (re.compile(r'\btatala\b', re.IGNORECASE), 'talata'),
    (re.compile(r'\btalta\b', re.IGNORECASE), 'talata'),
    (re.compile(r'\bmarina\b', re.IGNORECASE), 'maraina'),
    (re.compile(r'\blatsinainy\b', re.IGNORECASE), 'alatsinainy'),
    (re.compile(r'\blatsiny\b', re.IGNORECASE), 'alatsinainy'),
    (re.compile(r'\bsab\b', re.IGNORECASE), 'sabotsy'),
    (re.compile(r'\bzom\b', re.IGNORECASE), 'zoma'),
    (re.compile(r'\barobia\b', re.IGNORECASE), 'alarobia'),
    (re.compile(r'\bakamisy\b', re.IGNORECASE), 'alakamisy'),
    (re.compile(r'\balahad\b', re.IGNORECASE), 'alahady'),
)
_AT_HOUR_PATTERN = re.compile(r'@\s*(\d{1,2})\s*h?\b', re.IGNORECASE)
AM_HOUR_PATTERN = re.compile(
    r"(?:@|amin'?ny|ami'?ny|am)\s*(\d{1,2})\s*h?\b",
    re.IGNORECASE,
)
DELIVERY_PREFIX = re.compile(
    r'^(?:aterina|livraison|delivery|ao|à|a)\s+',
    re.IGNORECASE,
)
ISA_QUANTITY_PATTERN = re.compile(r'\bisa\s*[:=]\s*(\d{1,3})\b', re.IGNORECASE)

RELATIVE_DAY_PATTERN = re.compile(
    r'\b(afaka\s+rahampitso|apres[\s\-]?demain|rahampitso|rampitso|demain|androany|aujourdhui)\b',
    re.IGNORECASE,
)
RELATIVE_DAY_OFFSETS = {
    'androany': 0,
    'aujourdhui': 0,
    'rampitso': 1,
    'rahampitso': 1,
    'demain': 1,
    'afaka rahampitso': 2,
    'apres demain': 2,
    'apresdemain': 2,
}


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize('NFKD', value.lower())
    return ''.join(ch for ch in normalized if not unicodedata.combining(ch))


def _normalize_phone(value: str) -> str | None:
    digits = re.sub(r'\D', '', value or '')
    if digits.startswith('261') and len(digits) >= 12:
        digits = '0' + digits[3:]
    if len(digits) == 9 and digits.startswith('3'):
        digits = '0' + digits
    if len(digits) == 10 and digits.startswith('03'):
        return digits
    return None


def _looks_like_phone(value: str) -> bool:
    return _normalize_phone(value) is not None


def _parse_delivery_time(value: str | None) -> time | None:
    if not value:
        return None
    cleaned = value.strip()
    normalized = _normalize_text(cleaned)
    normalized = re.sub(r'apres[\s\-]?midi', 'apresmidi', normalized)
    morning_hint = bool(re.search(r'\b(matin|maraina)\b', normalized))
    delivery_hint = bool(re.search(r'\b(?:aterina|livraison|delivery|hatraiza)\b', normalized))

    def _hour_for_delivery(hour: int) -> int:
        if morning_hint:
            return hour
        if 1 <= hour <= 6:
            return hour + 12
        return hour

    # « 2 atoandro », « 14h maraina »
    period_hour = RELATIVE_HOUR_PERIOD_PATTERN.search(normalized)
    if period_hour:
        hour = int(period_hour.group(1))
        period = _normalize_text(period_hour.group(2)).replace(' ', '').replace('-', '')
        if period in {'atoandro', 'aprem', 'apresmidi'} and hour <= 12:
            hour = hour + 12 if hour < 12 else hour
        elif period in {'matin', 'maraina'}:
            pass
        if 0 <= hour <= 23:
            return time(hour, 0)

    # Période seule : matin / maraina / atoandro / hariva
    period_only = PERIOD_PATTERN.search(normalized)
    if period_only and not re.search(r'\d', normalized):
        key = _normalize_text(period_only.group(1)).replace(' ', '').replace('-', '')
        key = 'apresmidi' if key.startswith('apres') else key
        return PERIOD_DEFAULT_TIMES.get(key)

    am_hour = AM_HOUR_PATTERN.search(normalized)
    if am_hour:
        hour = int(am_hour.group(1))
        if morning_hint:
            pass
        elif 1 <= hour <= 6:
            hour += 12
        elif hour >= 7:
            pass
        if 0 <= hour <= 23:
            return time(hour, 0)

    compact = cleaned.lower().replace(' ', '')
    match = re.match(r'^(\d{1,2})[h:](\d{2})$', compact)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if 1 <= hour <= 6 and not morning_hint:
            hour += 12
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return time(hour, minute)
    match = re.match(r'^(\d{1,2})[hH]$', cleaned.strip())
    if match:
        hour = _hour_for_delivery(int(match.group(1)))
        if 0 <= hour <= 23:
            return time(hour, 0)
    try:
        parsed = datetime.strptime(cleaned.strip(), '%H:%M').time()
        hour = parsed.hour
        if 1 <= hour <= 6 and not morning_hint:
            return time(hour + 12, parsed.minute)
        return parsed
    except ValueError:
        return None


def _looks_like_time(value: str) -> bool:
    return _parse_delivery_time(value) is not None


def _next_weekday(target_weekday: int, reference: date | None = None) -> date:
    """Prochaine occurrence du jour (y compris aujourd'hui).

    Jeudi + « zoma » → vendredi (demain), pas vendredi dans 8 jours.
    """
    reference = reference or timezone.localdate()
    delta = (target_weekday - reference.weekday()) % 7
    return reference + timedelta(days=delta)


def _parse_relative_day_date(value: str, reference: date | None = None) -> date | None:
    """« rampitso », « rahampitso », « demain », « androany » → date relative."""
    reference = reference or timezone.localdate()
    normalized = _normalize_text(value or '')
    normalized = CONNECTOR_PATTERN.sub(' ', normalized)
    normalized = re.sub(r'apres[\s\-]?demain', 'apres demain', normalized)
    normalized = re.sub(r'[\s,;/\-–—]+', ' ', normalized).strip()
    match = RELATIVE_DAY_PATTERN.search(normalized)
    if not match:
        return None
    key = _normalize_text(match.group(1))
    key = re.sub(r'\s+', ' ', key).strip()
    if key.startswith('apres'):
        key = 'apres demain'
    offset = RELATIVE_DAY_OFFSETS.get(key)
    if offset is None:
        return None
    leftover = RELATIVE_DAY_PATTERN.sub(' ', normalized)
    leftover = PERIOD_PATTERN.sub(' ', leftover)
    leftover = WEEKDAY_PATTERN.sub(' ', leftover)
    leftover = re.sub(r'\d{1,2}\s*[hH]?\s*', ' ', leftover)
    leftover = re.sub(r'\s+', ' ', leftover).strip()
    if leftover and len(leftover) > 12:
        return None
    return reference + timedelta(days=offset)


def _parse_weekday_date(value: str, reference: date | None = None) -> date | None:
    """« alatsinainy », « samedi », « amin'ny zoma » → date du prochain jour nommé."""
    reference = reference or timezone.localdate()
    normalized = _normalize_text(value or '')
    normalized = CONNECTOR_PATTERN.sub(' ', normalized)
    normalized = re.sub(r'[\s,;/\-–—]+', ' ', normalized).strip()
    match = WEEKDAY_PATTERN.search(normalized)
    if not match:
        return None
    # Évite de prendre un jour noyé dans une longue adresse sans intention date.
    leftover = WEEKDAY_PATTERN.sub(' ', normalized)
    leftover = PERIOD_PATTERN.sub(' ', leftover)
    leftover = RELATIVE_DAY_PATTERN.sub(' ', leftover)
    leftover = re.sub(r'\d{1,2}\s*[hH]?\s*', ' ', leftover)
    leftover = re.sub(r'\s+', ' ', leftover).strip()
    if leftover and len(leftover) > 12:
        return None
    return _next_weekday(WEEKDAY_NAMES[match.group(1).lower()], reference)


def _parse_french_date(value: str, reference: date | None = None) -> date | None:
    reference = reference or timezone.localdate()
    cleaned = value.strip()
    normalized = _normalize_text(cleaned)

    for fmt in ('%d/%m/%Y', '%d-%m-%Y', '%Y-%m-%d', '%d/%m/%y'):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue

    match = re.match(r'^(\d{1,2})\s+([a-z]+)(?:\s+(\d{4}))?$', normalized)
    if match:
        day = int(match.group(1))
        month = FRENCH_MONTHS.get(match.group(2))
        year = int(match.group(3)) if match.group(3) else reference.year
        if month and 1 <= day <= 31:
            try:
                parsed = date(year, month, day)
                if not match.group(3) and parsed < reference:
                    parsed = date(year + 1, month, day)
                return parsed
            except ValueError:
                return None

    relative = _parse_relative_day_date(cleaned, reference)
    if relative:
        return relative
    return _parse_weekday_date(cleaned, reference)


def _looks_like_date(value: str) -> bool:
    return _parse_french_date(value) is not None


def _extract_inline_date_time(value: str) -> tuple[str | None, str | None]:
    """Extrait date/heure d'une ligne mixte.

    Ex. '12 mai 14h', 'samedi matin', 'zoma maraina', 'rampitso',
    \"alahady ami'ny 2 atoandro\", \"amin'ny alatsinainy\".
    """
    remaining = value.strip()
    date_part = None
    time_part = None

    normalized_for_period = _normalize_text(remaining)
    normalized_for_period = re.sub(r'apres[\s\-]?midi', 'apresmidi', normalized_for_period)

    # Heure liée à une période : « 2 atoandro »
    period_hour = RELATIVE_HOUR_PERIOD_PATTERN.search(normalized_for_period)
    if period_hour:
        time_part = period_hour.group(0).strip()
        remaining = RELATIVE_HOUR_PERIOD_PATTERN.sub(' ', remaining)
    else:
        period_match = PERIOD_PATTERN.search(normalized_for_period)
        if period_match:
            time_part = period_match.group(1)
            remaining = PERIOD_PATTERN.sub(' ', remaining)
            # « maraina am 9 » : heure explicite après la période.
            am_after_period = AM_HOUR_PATTERN.search(_normalize_text(remaining))
            if am_after_period:
                time_part = f"{time_part} am {am_after_period.group(1)}"
                remaining = AM_HOUR_PATTERN.sub(' ', remaining)

        time_match = re.search(r'(\d{1,2}\s*[hH:]\s*\d{0,2})', remaining)
        if time_match and not time_part:
            time_part = re.sub(r'\s+', '', time_match.group(1).strip())
            remaining = remaining.replace(time_match.group(0), ' ')

    # Nettoie les connecteurs restants (« à », « amin'ny », « ami'ny », tirets…).
    remaining = CONNECTOR_PATTERN.sub(' ', remaining)
    remaining = re.sub(r'[\s,;/\-–—]+', ' ', remaining).strip()

    if remaining and _looks_like_date(remaining):
        date_part = remaining
    elif not remaining and time_part:
        # « samedi matin » / « rampitso maraina » : jour retiré avec la période.
        weekday_match = WEEKDAY_PATTERN.search(_normalize_text(value))
        relative_match = RELATIVE_DAY_PATTERN.search(_normalize_text(value))
        if weekday_match:
            date_part = weekday_match.group(1)
        elif relative_match:
            date_part = relative_match.group(1)

    return date_part, time_part


def _normalize_address_segment(value: str) -> str:
    cleaned = (value or '').strip()
    cleaned = DELIVERY_PREFIX.sub('', cleaned).strip()
    return cleaned


def _merge_parsed_fields(base: dict[str, str], extra: dict[str, str]) -> dict[str, str]:
    merged = dict(base)
    for key, value in extra.items():
        if value and not merged.get(key):
            merged[key] = value
    return merged


def _strip_quantity_from_text(text: str) -> str:
    work = (text or '').strip()
    work = re.sub(
        r'[,\s]*(?:isa|qty|qte|quantit[eé]|nombre)\s*[:=]?\s*\d{1,3}\s*$',
        '',
        work,
        flags=re.IGNORECASE,
    )
    work = re.sub(r'[,\s]*:\d{1,3}\s*$', '', work)
    if not re.search(r'(?:am|amin\'?ny)\s+\d{1,3}\s*$', work, re.IGNORECASE):
        work = re.sub(r'(?<![hH])\s+\d{1,3}\s*$', '', work)
    return work.strip()


def _extract_time_token(text: str) -> tuple[str | None, str]:
    work = (text or '').strip()
    if not work:
        return None, work

    period_hour = re.search(
        r'\b(matin|maraina|atoandro|aprem|apres[\s\-]?midi|soir|hariva)\s+(?:am\s+)?(\d{1,2})\s*h?\b',
        work,
        re.IGNORECASE,
    )
    if period_hour:
        token = f'{period_hour.group(1)} am {period_hour.group(2)}'
        work = (work[:period_hour.start()] + ' ' + work[period_hour.end():]).strip()
        return token, work

    hour_match = re.search(r'(\d{1,2}\s*[hH])(?:\s*(\d{2}))?\b', work)
    if hour_match:
        token = hour_match.group(0).replace(' ', '')
        work = (work[:hour_match.start()] + ' ' + work[hour_match.end():]).strip()
        return token, work

    am_match = AM_HOUR_PATTERN.search(work)
    if am_match:
        normalized_work = _normalize_text(work)
        period_match = PERIOD_PATTERN.search(normalized_work)
        hour = int(am_match.group(1))
        if period_match:
            period = period_match.group(1)
        elif re.search(r'\b(?:aterina|livraison|delivery|hatraiza)\b', normalized_work):
            period = 'atoandro'
        elif 1 <= hour <= 6:
            period = 'atoandro'
        elif 7 <= hour <= 11:
            period = 'maraina'
        else:
            period = 'atoandro'
        token = f'{period} am {hour}'
        end = am_match.end()
        ora_tail = re.match(r'\s*ora\b', work[end:], re.IGNORECASE)
        if ora_tail:
            end += ora_tail.end()
        work = (work[:am_match.start()] + ' ' + work[end:]).strip()
        return token, work

    _, period_time = _extract_inline_date_time(work)
    if period_time:
        work = work.replace(period_time, ' ').strip()
        return period_time, work
    return None, work


def _extract_date_token(text: str) -> tuple[str | None, str]:
    work = (text or '').strip()
    if not work:
        return None, work

    date_match = re.search(r'\b(\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4})\b', work)
    if date_match and _looks_like_date(date_match.group(1)):
        token = date_match.group(1)
        work = (work[:date_match.start()] + ' ' + work[date_match.end():]).strip()
        return token, work

    weekday_match = WEEKDAY_PATTERN.search(work)
    if weekday_match:
        token = weekday_match.group(1)
        work = (work[:weekday_match.start()] + ' ' + work[weekday_match.end():]).strip()
        return token, work

    relative_match = RELATIVE_DAY_PATTERN.search(_normalize_text(work))
    if relative_match:
        token = relative_match.group(1)
        work = RELATIVE_DAY_PATTERN.sub(' ', work, count=1).strip()
        return token, work

    inline_date, _ = _extract_inline_date_time(work)
    if inline_date and _looks_like_date(inline_date):
        work = work.replace(inline_date, ' ').strip()
        return inline_date, work
    return None, work


def _scrub_extracted_text(text: str, *tokens: str | None) -> str:
    scrub = text or ''
    for token in tokens:
        if token:
            scrub = scrub.replace(token, ' ')
    scrub = WEEKDAY_PATTERN.sub(' ', scrub)
    scrub = PERIOD_PATTERN.sub(' ', scrub)
    scrub = AM_HOUR_PATTERN.sub(' ', scrub)
    scrub = re.sub(r'\b\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}\b', ' ', scrub)
    scrub = DELIVERY_PREFIX.sub('', scrub)
    scrub = re.sub(r'\b(?:livraison|delivery|aterina|ao|isa|qty|qte|am|@)\b', ' ', scrub, flags=re.I)
    scrub = CONNECTOR_PATTERN.sub(' ', scrub)
    return re.sub(r'[\s,;/\-–—]+', ' ', scrub).strip()


def _is_greeting_token(text: str) -> bool:
    cleaned = (text or '').strip(' ,;!?.')
    return bool(cleaned and GREETING_TOKEN.match(cleaned))


def _normalize_confirmation_input(text: str) -> str:
    """Corrige abréviations et fautes fréquentes avant extraction."""
    work = (text or '').strip()
    work = re.sub(r'[?!]+$', '', work).strip()
    work = _AT_HOUR_PATTERN.sub(r'am \1', work)
    work = re.sub(r'@(?=\d)', 'am ', work)
    for pattern, replacement in CONFIRMATION_TYPO_FIXES:
        work = pattern.sub(replacement, work)
    return work.strip()


def _strip_greeting_prefix(text: str) -> str:
    return GREETING_PREFIX.sub('', (text or '').strip()).strip(' ,;')


def _canonicalize_confirmation_text(text: str) -> str:
    work = _normalize_confirmation_input(text)
    work = _strip_greeting_prefix(work)
    work = _strip_quantity_from_text(work)
    return work.strip()


def _is_plausible_nom(value: str | None) -> bool:
    cleaned = (value or '').strip(' ,;!?.@')
    if not cleaned or _is_greeting_token(cleaned):
        return False
    if cleaned.startswith('@') or not re.search(r'[a-zA-Zàâäéèêëïîôùûüç]', cleaned):
        return False
    if len(cleaned) <= 2 and cleaned.isalpha():
        return False
    if _looks_like_date(cleaned) or _looks_like_time(cleaned) or _looks_like_phone(cleaned):
        return False
    return True


def _sanitize_parsed_fields(fields: dict[str, str]) -> dict[str, str]:
    cleaned = dict(fields)
    if cleaned.get('nom') and not _is_plausible_nom(cleaned['nom']):
        cleaned.pop('nom', None)
    return cleaned


def _parse_freeform_spaced_confirmation(text: str) -> dict[str, str]:
    cleaned = _canonicalize_confirmation_text(text)
    if not cleaned or '\n' in cleaned or ',' in cleaned or ';' in cleaned:
        return {}

    phone_match = PHONE_LOOSE_PATTERN.search(cleaned)
    if not phone_match:
        return {}

    phone = _normalize_phone(phone_match.group(0))
    if not phone:
        return {}

    before = cleaned[:phone_match.start()].strip()
    after = cleaned[phone_match.end():].strip()
    if not before:
        return {}
    after = re.sub(
        r'^(?:aterina|livraison|delivery)\s+(?:am|amin\'?ny)?\s*',
        '',
        after,
        flags=re.IGNORECASE,
    ).strip()
    after = DELIVERY_PREFIX.sub('', after).strip()

    fields: dict[str, str] = {'telephone': phone}
    if before:
        words = [word for word in before.split() if word]
        if len(words) >= 2:
            fields['nom'] = words[0]
            fields['adresse'] = _normalize_address_segment(' '.join(words[1:]))
        elif len(words) == 1 and _is_plausible_nom(words[0]):
            fields['nom'] = words[0]

    if after:
        time_token, work = _extract_time_token(after)
        if time_token:
            fields['heure_livraison'] = time_token
        date_token, work = _extract_date_token(work or after)
        if date_token:
            fields['date_livraison'] = date_token
        elif not time_token:
            period_match = PERIOD_PATTERN.search(_normalize_text(after))
            if period_match:
                fields['heure_livraison'] = period_match.group(1)
        if not fields.get('adresse'):
            addr = _normalize_address_segment(_scrub_extracted_text(work))
            if addr:
                fields['adresse'] = addr

    return _sanitize_parsed_fields(fields)


def _parse_structured_comma_confirmation(text: str) -> dict[str, str]:
    """Format courant : [salut], nom, tél, adresse[, date/heure][, isa]."""
    cleaned = (text or '').strip()
    if '\n' in cleaned:
        return {}

    parts = [part.strip() for part in re.split(r'[,;]', cleaned) if part.strip()]
    parts = [part for part in parts if not _is_greeting_token(part)]
    if parts and _is_quantity_line(parts[-1]):
        parts = parts[:-1]
    if len(parts) < 3:
        return {}

    phone_idx = next((i for i, part in enumerate(parts) if _looks_like_phone(part)), None)
    if phone_idx is None or phone_idx < 1:
        return {}

    name = parts[phone_idx - 1]
    if _is_greeting_token(name) or _looks_like_phone(name):
        return {}

    phone = _normalize_phone(parts[phone_idx])
    if not phone:
        return {}

    fields: dict[str, str] = {'nom': name, 'telephone': phone}
    tail = parts[phone_idx + 1:]
    if not tail:
        return fields

    if len(tail) == 1:
        segment = DELIVERY_PREFIX.sub('', tail[0]).strip()
        time_token, work = _extract_time_token(segment)
        if time_token:
            fields['heure_livraison'] = time_token
        date_token, work = _extract_date_token(work)
        if date_token:
            fields['date_livraison'] = date_token
        addr = _normalize_address_segment(_scrub_extracted_text(work))
        if addr:
            fields['adresse'] = addr
        return fields

    addr = _normalize_address_segment(DELIVERY_PREFIX.sub('', tail[0]).strip())
    if addr:
        fields['adresse'] = addr
    datetime_blob = ' '.join(tail[1:])
    inline_date, inline_time = _extract_inline_date_time(datetime_blob)
    if inline_date:
        fields['date_livraison'] = inline_date
    if inline_time:
        fields['heure_livraison'] = inline_time
    if not inline_date:
        date_token, _ = _extract_date_token(datetime_blob)
        if date_token:
            fields['date_livraison'] = date_token
    if not inline_time:
        time_token, _ = _extract_time_token(datetime_blob)
        if time_token:
            fields['heure_livraison'] = time_token
    return fields


def _extract_fields_from_fragment(text: str) -> dict[str, str]:
    """Extrait nom/tél/adresse/date/heure d'un fragment libre (espaces, virgules…)."""
    fields: dict[str, str] = {}
    work = (text or '').strip()
    if not work:
        return fields

    work = _strip_greeting_prefix(work)
    work = _strip_quantity_from_text(work)

    phone_match = PHONE_LOOSE_PATTERN.search(work)
    if phone_match:
        phone = _normalize_phone(phone_match.group(0))
        if phone:
            fields['telephone'] = phone
        work = (work[:phone_match.start()] + ' ' + work[phone_match.end():]).strip()

    time_token, work = _extract_time_token(work)
    if time_token:
        fields['heure_livraison'] = time_token

    date_token, work = _extract_date_token(work)
    if date_token:
        fields['date_livraison'] = date_token

    scrub = _scrub_extracted_text(work, fields.get('telephone'))
    words = [word for word in scrub.split() if word and not word.isdigit()]
    if words:
        if len(words) == 1:
            if fields.get('telephone') and not fields.get('adresse'):
                fields['adresse'] = _normalize_address_segment(words[0])
            elif not fields.get('nom') and _is_plausible_nom(words[0]):
                fields['nom'] = words[0]
            elif not fields.get('adresse'):
                fields['adresse'] = _normalize_address_segment(words[0])
        else:
            if not fields.get('nom') and _is_plausible_nom(words[0]):
                fields.setdefault('nom', words[0])
            fields.setdefault('adresse', _normalize_address_segment(' '.join(words[1:])))
    return _sanitize_parsed_fields(fields)


def _parse_phone_adresse_datetime_commas(text: str) -> dict[str, str]:
    cleaned = (text or '').strip()
    if '\n' in cleaned:
        return {}
    parts = [part.strip() for part in re.split(r'[,;]', cleaned) if part.strip()]
    if len(parts) != 3:
        return {}
    phone_part, middle, tail = parts
    if not _looks_like_phone(phone_part) or _looks_like_phone(middle):
        return {}
    fields: dict[str, str] = {
        'telephone': _normalize_phone(phone_part) or phone_part,
        'adresse': _normalize_address_segment(middle),
    }
    tail_fields = _extract_fields_from_fragment(tail)
    for key in ('date_livraison', 'heure_livraison'):
        if tail_fields.get(key):
            fields[key] = tail_fields[key]
    return fields


def _split_confirmation_lines(text: str) -> list[str]:
    """Découpe un message en lignes — y compris format virgules sur une seule ligne."""
    cleaned = (text or '').strip()
    if not cleaned:
        return []
    raw_lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if len(raw_lines) > 1:
        return raw_lines
    single = raw_lines[0]
    if ',' not in single and ';' not in single:
        return [single]
    parts = [part.strip() for part in re.split(r'[,;]', single) if part.strip()]
    return parts if len(parts) >= 2 else [single]


def _expand_confirmation_segment(segment: str) -> list[str]:
    """Sépare tél / quantité / reste dans un segment mixte (ex. « 038… aterina Ivato »)."""
    expanded: list[str] = []
    remaining = segment.strip()
    if not remaining:
        return expanded

    remaining = _strip_greeting_prefix(remaining) or remaining

    phone_match = PHONE_LOOSE_PATTERN.search(remaining)
    if phone_match:
        phone = _normalize_phone(phone_match.group(0))
        if phone:
            expanded.append(phone)
        remaining = (
            remaining[:phone_match.start()] + ' ' + remaining[phone_match.end():]
        ).strip(' ,;/-')

    qty_match = ISA_QUANTITY_PATTERN.search(remaining)
    if qty_match:
        expanded.append(qty_match.group(0))
        remaining = remaining[:qty_match.start()].strip(' ,;/-')

    if remaining:
        mixed = _extract_fields_from_fragment(remaining)
        mixed_parts = [
            mixed[key]
            for key in ('nom', 'adresse', 'date_livraison', 'heure_livraison')
            if mixed.get(key)
        ]
        if len(mixed_parts) >= 2:
            expanded.extend(mixed_parts)
        else:
            expanded.append(remaining)
    return expanded or [segment]


def _prepare_confirmation_lines(text: str) -> list[str]:
    lines: list[str] = []
    for line in _split_confirmation_lines(text):
        for part in _expand_confirmation_segment(line):
            part = part.strip()
            if not part or _is_greeting_token(part):
                continue
            lines.append(part)
    return lines


def parse_confirmation_text(text: str) -> dict[str, str]:
    """
    Extrait nom, téléphone, adresse, date et heure depuis un message privé.
    Accepte les formats étiquetés ou libres, ex. :
      Lova
      Bypass
      12 mai
      14h
    """
    cleaned = _normalize_confirmation_input((text or '').strip())
    if not cleaned:
        return {}

    parsed: dict[str, str] = {}
    for field, pattern in FIELD_PATTERNS.items():
        match = pattern.search(cleaned)
        if match:
            parsed[field] = match.group(1).strip().split('\n')[0].strip()

    if len(parsed) >= 3:
        return parsed

    # Téléphone, adresse, date/heure sur une ligne séparés par des virgules.
    if '\n' not in cleaned:
        structured = _parse_structured_comma_confirmation(cleaned)
        if structured.get('nom') and structured.get('telephone'):
            return structured

        comma_fields = _parse_phone_adresse_datetime_commas(cleaned)
        if comma_fields.get('telephone') and comma_fields.get('adresse'):
            return comma_fields

    # Message sur une seule ligne : formats très variables (virgules, espaces, mélangés).
    if '\n' not in cleaned:
        spaced = _parse_freeform_spaced_confirmation(cleaned)
        if spaced.get('telephone') and (spaced.get('nom') or spaced.get('adresse')):
            return spaced

        freeform = _extract_fields_from_fragment(cleaned)
        if freeform.get('telephone') or (
            freeform.get('nom') and (freeform.get('adresse') or freeform.get('date_livraison'))
        ):
            return freeform

    lines = _prepare_confirmation_lines(cleaned)
    if not lines:
        return parsed

    classified = {'phones': [], 'dates': [], 'times': [], 'others': []}
    for line in lines:
        if _is_quantity_line(line):
            continue
        inline_date, inline_time = _extract_inline_date_time(line)
        if inline_date:
            classified['dates'].append(inline_date)
            if inline_time:
                classified['times'].append(inline_time)
            continue
        if inline_time and not inline_date:
            classified['times'].append(inline_time)
            continue
        if _looks_like_phone(line):
            phone = _normalize_phone(line)
            if phone:
                classified['phones'].append(phone)
            continue
        if _looks_like_time(line):
            classified['times'].append(line)
            continue
        if _looks_like_date(line):
            classified['dates'].append(line)
            continue
        classified['others'].append(line)

    if classified['phones']:
        parsed.setdefault('telephone', classified['phones'][0])
    if classified['dates']:
        parsed.setdefault('date_livraison', classified['dates'][0])
    if classified['times']:
        parsed.setdefault('heure_livraison', classified['times'][0])

    others = classified['others']
    if others:
        first_other = next((item for item in others if _is_plausible_nom(item)), None)
        if first_other:
            parsed.setdefault('nom', first_other)
        if len(others) > 1:
            rest = [item for item in others if item != parsed.get('nom')]
            if rest:
                parsed.setdefault('adresse', _normalize_address_segment(' '.join(rest)))
        elif len(others) == 1 and not parsed.get('adresse'):
            # Une seule ligne texte restante sans téléphone/date → probablement l'adresse/quartier
            if parsed.get('nom') and parsed.get('telephone') and parsed.get('date_livraison'):
                parsed.setdefault('adresse', _normalize_address_segment(others[0]))
            elif parsed.get('nom') and (parsed.get('date_livraison') or parsed.get('telephone')):
                if not _looks_like_date(others[0]) and not _looks_like_phone(others[0]):
                    if parsed['nom'] == others[0] and len(lines) >= 2:
                        pass
                    else:
                        addr = others[0] if parsed.get('nom') != others[0] else ''
                        parsed.setdefault('adresse', _normalize_address_segment(addr))

    # Cas typique Madagascar : Nom / Quartier / Date [/ Heure]
    if len(lines) >= 3 and not parsed.get('adresse'):
        if (
            parsed.get('nom')
            and parsed.get('date_livraison')
            and len(classified['others']) >= 2
        ):
            parsed['adresse'] = _normalize_address_segment(classified['others'][1])
        elif len(classified['others']) == 2 and parsed.get('date_livraison'):
            parsed.setdefault('nom', classified['others'][0])
            parsed.setdefault('adresse', _normalize_address_segment(classified['others'][1]))
        elif len(classified['others']) == 1 and parsed.get('nom') and parsed.get('date_livraison'):
            # nom + date détectés, 1 ligne quartier restante
            for line in classified['others']:
                if line != parsed.get('nom'):
                    parsed.setdefault('adresse', _normalize_address_segment(line))

    # Reconstruction explicite 3 lignes : Nom / Adresse / Date
    if len(lines) == 3 and not parsed.get('telephone'):
        if _looks_like_date(lines[2]) and not _looks_like_phone(lines[1]):
            parsed['nom'] = lines[0]
            parsed['adresse'] = lines[1]
            parsed['date_livraison'] = lines[2]

    if len(lines) == 4 and not parsed.get('telephone'):
        if _looks_like_date(lines[2]) and _looks_like_time(lines[3]) and not _looks_like_phone(lines[1]):
            parsed['nom'] = lines[0]
            parsed['adresse'] = lines[1]
            parsed['date_livraison'] = lines[2]
            parsed['heure_livraison'] = lines[3]

    if sum(1 for key in ('nom', 'telephone', 'adresse', 'date_livraison') if parsed.get(key)) < 3:
        parsed = _merge_parsed_fields(parsed, _extract_fields_from_fragment(cleaned))

    return _sanitize_parsed_fields(parsed)


def _parse_delivery_date(value: str | None):
    if not value:
        return None
    return _parse_french_date(value)


def detect_client_channel(client: Client) -> str:
    if client.facebook_id:
        return 'Facebook'
    if client.tiktok_id:
        return 'TikTok'
    return 'Inconnu'


def find_pending_commande(
    client: Client,
    vendeur: Vendeur | None = None,
    *,
    prefer_active_live: bool = True,
) -> Commande | None:
    queryset = (
        Commande.objects.select_related('produit', 'produit__vendeur', 'client', 'variante', 'live')
        .filter(client=client, statut=Commande.STATUT_JP_CAPTURE)
    )
    if vendeur:
        queryset = queryset.filter(produit__vendeur=vendeur)
    if prefer_active_live:
        queryset = queryset.order_by(
            models.Case(
                models.When(live__statut=Live.STATUT_EN_COURS, then=0),
                default=1,
            ),
            'ordre_jp',
            '-date_creation',
        )
    else:
        queryset = queryset.order_by('ordre_jp', '-date_creation')
    return queryset.first()


def claim_masked_facebook_client(
    *,
    real_facebook_id: str,
    vendeur: Vendeur | None = None,
) -> Client | None:
    """Rattache un PSID Messenger à un client créé sans auteur Meta (`fb_comment:…`).

    Quand un admin (ou tout auteur masqué) JP, on crée parfois un client de repli.
    Au premier DM réel, on réécrit facebook_id avec le vrai PSID pour enchaîner la confirmation.
    """
    if not real_facebook_id or real_facebook_id.startswith('fb_comment:'):
        return None

    queryset = (
        Commande.objects.select_related('client', 'produit__vendeur')
        .filter(
            statut=Commande.STATUT_JP_CAPTURE,
            client__facebook_id__startswith='fb_comment:',
        )
        .order_by('-date_creation')
    )
    if vendeur:
        queryset = queryset.filter(produit__vendeur=vendeur)

    commande = queryset.first()
    if not commande:
        return None

    client = commande.client
    # Évite d'écraser si le PSID appartient déjà à un autre client.
    if Client.objects.filter(facebook_id=real_facebook_id).exclude(pk=client.pk).exists():
        return None

    client.facebook_id = real_facebook_id
    if client.nom.startswith('Client Facebook'):
        client.nom = 'Client Facebook'
    client.save(update_fields=['facebook_id', 'nom'])
    return client


# Statuts à partir desquels un client peut encore annuler sa commande.
# (On exclut volontairement EN_LIVRAISON, LIVRE et ANNULE : trop tard ou déjà fait.)
CANCELLABLE_STATUSES = (
    Commande.STATUT_JP_CAPTURE,
    Commande.STATUT_CONFIRME,
    Commande.STATUT_PREPARE,
)


def find_cancellable_commande(client: Client, vendeur: Vendeur | None = None) -> Commande | None:
    """Dernière commande encore annulable du client (JP en attente, confirmée ou préparée)."""
    queryset = (
        Commande.objects.select_related('produit', 'produit__vendeur', 'client', 'variante', 'live')
        .filter(client=client, statut__in=CANCELLABLE_STATUSES)
        .order_by('-date_creation')
    )
    if vendeur:
        queryset = queryset.filter(produit__vendeur=vendeur)
    return queryset.first()


def _stock_remaining_for(commande: Commande) -> int | None:
    """Stock encore disponible pour cette commande (après file JP devant elle).

    None = pas de variante / stock non applicable (éligible par défaut).
    La file « devant » est limitée au même live quand possible.
    """
    variante = commande._get_stock_variante()
    if not variante:
        return None

    ahead = Commande.objects.filter(
        produit=commande.produit,
        variante=commande.variante,
        statut=Commande.STATUT_JP_CAPTURE,
        ordre_jp__lt=commande.ordre_jp,
    ).exclude(pk=commande.pk)
    if commande.live_id:
        ahead = ahead.filter(live_id=commande.live_id)

    qty_ahead = sum(c.quantite_effective for c in ahead)
    return max(variante.stock - qty_ahead, 0)


def cancel_commande_public(commande: Commande) -> dict[str, Any]:
    """Annule une commande depuis le formulaire public (JP / confirmée / préparée)."""
    if commande.statut not in CANCELLABLE_STATUSES:
        raise OrderConfirmationError(
            f'La commande #{commande.id} ne peut plus être annulée '
            f'(statut : {commande.get_statut_display()}).',
            status_code=409,
        )

    commande.statut = Commande.STATUT_ANNULE
    commande.save(update_fields=['statut'])

    from .order_messaging import send_order_cancelled_message

    outbound = send_order_cancelled_message(commande)
    return {
        'status': 'Commande annulée',
        'annule': True,
        'commande_id': commande.id,
        'commande': CommandeSerializer(commande).data,
        'message_annulation': outbound.get('content'),
        'message_delivery': outbound.get('delivery'),
    }


def resolve_page_for_commande(commande: Commande) -> PageFacebook | None:
    vendeur = commande.produit.vendeur
    if commande.live_id and commande.live.pages_facebook:
        for item in commande.live.pages_facebook:
            page = (
                PageFacebook.objects.filter(vendeur=vendeur, nom=item).first()
                or PageFacebook.objects.filter(vendeur=vendeur, page_id=str(item)).first()
            )
            if page and page.access_token:
                return page

    # Page principale du vendeur (évite de tomber sur une autre page « .first() »).
    if vendeur.facebook_page_id:
        primary = (
            PageFacebook.objects.filter(vendeur=vendeur, page_id=str(vendeur.facebook_page_id))
            .exclude(access_token__isnull=True)
            .exclude(access_token='')
            .first()
        )
        if primary:
            return primary

    return (
        PageFacebook.objects.filter(vendeur=vendeur, statut=PageFacebook.STATUT_PRET)
        .exclude(access_token__isnull=True)
        .exclude(access_token='')
        .order_by('id')
        .first()
    )


def link_messenger_sender_to_client(sender_id: str, vendeur: Vendeur | None) -> Client | None:
    """Rattache un PSID Messenger au client qui répond après capture JP.

    L'id « from » d'un commentaire live diffère du PSID Messenger : le webhook
    ``messages`` envoie le PSID. On relie d'abord la commande JP récente sans
    réponse inbound sur un live en cours (celui qui vient de recevoir le message
    de confirmation), sinon une commande JP unique en cours.
    """
    if not vendeur:
        return None

    psid = str(sender_id)
    pending = (
        Commande.objects.select_related('client', 'live')
        .filter(
            statut=Commande.STATUT_JP_CAPTURE,
            produit__vendeur=vendeur,
        )
        .order_by(
            models.Case(
                models.When(live__statut=Live.STATUT_EN_COURS, then=0),
                default=1,
            ),
            '-date_creation',
        )
    )

    for cmd in pending[:25]:
        has_inbound = Message.objects.filter(
            commande=cmd,
            direction=Message.DIRECTION_INBOUND,
        ).exists()
        if has_inbound:
            continue
        client = cmd.client
        if client.facebook_id == psid:
            return client
        old_id = client.facebook_id
        client.facebook_id = psid
        client.save(update_fields=['facebook_id'])
        logger.info(
            'Client #%s : PSID Messenger relié %s -> %s (commande #%s sans réponse)',
            client.pk,
            old_id,
            psid,
            cmd.pk,
        )
        return client

    if pending.count() == 1:
        client = pending.first().client
        if client.facebook_id != psid:
            logger.info(
                'Client #%s : PSID Messenger relié %s -> %s (commande JP unique en cours)',
                client.pk,
                client.facebook_id,
                psid,
            )
            client.facebook_id = psid
            client.save(update_fields=['facebook_id'])
        return client

    return None


CANCELLATION_PATTERNS = [
    re.compile(r'\ban+u+l', re.IGNORECASE),
    re.compile(r'\bje\s+(?:ne\s+)?(?:veux|prends?|prend)\s+plus\b', re.IGNORECASE),
    re.compile(r'\bne\s+(?:veux|prends?|prend)\s+plus\b', re.IGNORECASE),
    re.compile(r'\bplus\s+besoin\b', re.IGNORECASE),
    re.compile(r'\bnon\s+merci\b', re.IGNORECASE),
    # Malagasy
    re.compile(r'\btsy\s+(?:te|tia|mila|ila|ilaiko|ala?iko|maka|haka|mividy|hividy|haiko|alo)\b', re.IGNORECASE),
    re.compile(r'\bala?iko\s+(?:ndray|indray|intsony)\b', re.IGNORECASE),
    re.compile(r'\btsy\s+mila\s+intsony\b', re.IGNORECASE),
    re.compile(r'\b(?:ndray|indray)\s+(?:leizy|ilay)\b', re.IGNORECASE),
    re.compile(r'\bfoan[ao]\b', re.IGNORECASE),
    re.compile(r'\besory\b', re.IGNORECASE),
    re.compile(r'\bajanon[ay]\b', re.IGNORECASE),
    re.compile(r'\bavelao\b', re.IGNORECASE),
    re.compile(r'^\s*tsia\s*$', re.IGNORECASE),
    re.compile(r'\b(?:saika|te[\s\-]?|tia)\s*hanao\s+an+u+l', re.IGNORECASE),
    re.compile(r'\bhanao\s+an+u+l', re.IGNORECASE),
]


def _is_modification_cancellation(text: str) -> bool:
    """Annuler la dernière modification — pas la commande entière."""
    cleaned = (text or '').strip()
    if not cleaned:
        return False
    normalized = _normalize_text(cleaned)
    return any(
        pattern.search(cleaned) or pattern.search(normalized)
        for pattern in MODIFICATION_CANCEL_PATTERNS
    )


def _is_cancellation(text: str) -> bool:
    cleaned = (text or '').strip()
    if not cleaned:
        return False
    if _is_modification_cancellation(cleaned):
        return False
    normalized = _normalize_text(cleaned)
    return any(
        pattern.search(cleaned) or pattern.search(normalized)
        for pattern in CANCELLATION_PATTERNS
    )


THANKS_PATTERN = re.compile(
    r'\b(?:'
    r'mankasit?raka|mankasitrika|'
    r'misa+o?tra(?:\s+(?:betsaka|indrindra|tompoko|e|be))?|'
    r'misotra|misaotr|'
    r'merci(?:\s+beaucoup)?|thanks?(?:\s+you)?|'
    r'tonga\s+soa\s+ny\s+fisaorana'
    r')\b',
    re.IGNORECASE,
)

MODIFICATION_PATTERNS = [
    re.compile(r'\b(?:hanova|ovaina|ovay|ova|soloina|soloy)\b', re.IGNORECASE),
    re.compile(r'\b(?:modifi(?:er|e)|chang(?:er|e)|update|corrige[rz]?)\b', re.IGNORECASE),
    re.compile(r'\b(?:diso|incorrect|erreur)\b', re.IGNORECASE),
]

MODIFICATION_CANCEL_PATTERNS = [
    re.compile(
        r'\b(?:annul(?:er|e)|ajanony|ajanonay|foanony|esory)\s+(?:la|ny)?\s*(?:fanovana|modification|changement)\b',
        re.IGNORECASE,
    ),
    re.compile(
        r'\b(?:fanovana|modification|changement)\s+(?:tsy\s+)?(?:alefa|raiso|esorina|foanana)\b',
        re.IGNORECASE,
    ),
    re.compile(r'\btsy\s+alefa\s+ny\s+fanovana\b', re.IGNORECASE),
    re.compile(r'\bremettre\s+comme\s+avant\b', re.IGNORECASE),
    re.compile(r'\b(?:tadiavina|averina)\s+(?:ny\s+)?(?:ancien(?:ne)?|taloha|teo\s+aloha)\b', re.IGNORECASE),
    re.compile(r'\bajanony\s+ny\s+fanovana\b', re.IGNORECASE),
    re.compile(r'\bcancel(?:ler)?\s+(?:the\s+)?modif(?:ication)?\b', re.IGNORECASE),
]

SNAPSHOT_NUMERO_RELANCE = -1
SNAPSHOT_CANAL = '_snapshot'

REPRISE_PATTERNS = [
    re.compile(r'\b(?:reprend(?:re|s)?|reconfirme[rz]?)\b', re.IGNORECASE),
    re.compile(r'\bmbola\s+(?:te|tia|mila|hividy|mividy|te[\s\-]?hanao)\b', re.IGNORECASE),
    re.compile(r'\b(?:te|tia)[\s\-]?hividy\s+indr?ay\b', re.IGNORECASE),
    re.compile(r'\b(?:te|tia)[\s\-]?hanao\s+(?:commande\s+)?indr?ay\b', re.IGNORECASE),
    re.compile(r'\baverina\b', re.IGNORECASE),
    re.compile(r'\b(?:je\s+)?reprends?\b', re.IGNORECASE),
    re.compile(r'\bje\s+veux\s+(?:quand\s+m[eê]me|toujours)\b', re.IGNORECASE),
]

CONFIRMATION_ACK_PATTERNS = [
    re.compile(r'^(?:eka|ok|oka|oui|yes|valide|confirme)$', re.IGNORECASE),
    re.compile(r'\b(?:ekena|ekeko|jeka|metse|c\'?est\s+bon|ça\s+marche|tonga)\b', re.IGNORECASE),
]


def _looks_like_modification(text: str) -> bool:
    cleaned = (text or '').strip()
    if not cleaned:
        return False
    normalized = _normalize_text(cleaned)
    return any(p.search(cleaned) or p.search(normalized) for p in MODIFICATION_PATTERNS)


def _looks_like_reprise(text: str) -> bool:
    """Client veut reprendre après annulation (sans voler la place des suivants)."""
    cleaned = (text or '').strip()
    if not cleaned or len(cleaned) > 160:
        return False
    if _is_cancellation(cleaned):
        return False
    normalized = _normalize_text(cleaned)
    return any(p.search(cleaned) or p.search(normalized) for p in REPRISE_PATTERNS)


def _looks_like_confirmation_ack(text: str) -> bool:
    """Accord explicite pour confirmer une reprise avec infos déjà connues."""
    cleaned = (text or '').strip()
    if not cleaned or len(cleaned) > 120:
        return False
    if _looks_like_modification(cleaned) or _is_modification_cancellation(cleaned) or _looks_like_reprise(cleaned):
        return False
    if _looks_like_phone(cleaned) or _looks_like_date(cleaned) or _parse_delivery_time(cleaned):
        return False
    if _parse_quantity(cleaned, expecting=False):
        return False
    normalized = _normalize_text(cleaned)
    if any(p.search(cleaned) or p.search(normalized) for p in CONFIRMATION_ACK_PATTERNS):
        return True
    return any(p.search(cleaned) for p in ACCEPT_PARTIAL_PATTERNS) and len(cleaned) <= 40


def find_last_cancelled_commande(client: Client, vendeur: Vendeur | None = None) -> Commande | None:
    queryset = (
        Commande.objects.select_related('produit', 'produit__vendeur', 'client', 'variante', 'live')
        .filter(client=client, statut=Commande.STATUT_ANNULE)
        .order_by('-date_creation')
    )
    if vendeur:
        queryset = queryset.filter(produit__vendeur=vendeur)
    return queryset.first()


@transaction.atomic
def reopen_after_cancel(cancelled: Commande) -> dict[str, Any]:
    """Crée une NOUVELLE commande en fin de file (l'ancienne reste annulée).

    On ne reprend jamais la place déjà donnée aux suivants : nouvel ordre_jp,
    stock courant, file d'attente / offre partielle comme un nouveau client.
    Les infos client déjà connues sont réutilisées.
    """
    from django.db.models import Max

    if cancelled.statut != Commande.STATUT_ANNULE:
        raise OrderConfirmationError('Cette commande n\'est pas annulée.', status_code=409)

    # Déjà un JP ouvert sur la même déclinaison → on le réutilise.
    existing = (
        Commande.objects.select_for_update()
        .filter(
            client=cancelled.client,
            produit=cancelled.produit,
            variante=cancelled.variante,
            statut=Commande.STATUT_JP_CAPTURE,
        )
        .order_by('ordre_jp')
        .first()
    )
    if existing:
        commande = existing
    else:
        max_order = (
            Commande.objects.select_for_update()
            .filter(produit=cancelled.produit, variante=cancelled.variante)
            .aggregate(max_ordre=Max('ordre_jp'))['max_ordre']
            or 0
        )
        commande = Commande.objects.create(
            client=cancelled.client,
            produit=cancelled.produit,
            variante=cancelled.variante,
            live=cancelled.live,
            quantite=cancelled.quantite,
            ordre_jp=max_order + 1,
            statut=Commande.STATUT_JP_CAPTURE,
        )

    from .order_messaging import send_reprise_message, send_reprise_recap_message

    missing = _missing_confirmation_fields(commande)
    if missing:
        from .order_messaging import send_completion_request_message

        completion = send_completion_request_message(commande, missing)
        outbound = send_reprise_message(
            commande,
            ancienne_id=cancelled.id,
            outcome='infos',
        )
        return {
            'status': 'Reprise — nouvelle commande créée',
            'reprise': True,
            'complet': False,
            'ancienne_commande_id': cancelled.id,
            'commande': CommandeSerializer(commande).data,
            'champs_manquants': missing,
            'message_reprise': outbound.get('content'),
            'message_relance': completion.get('content'),
        }

    recap = send_reprise_recap_message(commande)
    outbound = send_reprise_message(
        commande,
        ancienne_id=cancelled.id,
        outcome='recap',
    )
    available = _available_stock_for_commande(commande)
    requested = commande.quantite_effective

    if _order_is_eligible(commande):
        return {
            'status': 'Reprise — confirmez vos informations',
            'reprise': True,
            'complet': False,
            'attente_confirmation': True,
            'ancienne_commande_id': cancelled.id,
            'commande': CommandeSerializer(commande).data,
            'champs_recus': _collected_fields_snapshot(commande),
            'message_reprise': outbound.get('content'),
            'message_recap': recap.get('content'),
            'message_delivery': recap.get('delivery'),
        }

    if available > 0 and requested > available:
        from .order_messaging import send_stock_partial_offer_message

        offer = send_stock_partial_offer_message(commande, available)
        return {
            'status': 'Reprise — stock partiel proposé',
            'reprise': True,
            'complet': False,
            'attente_confirmation': True,
            'ancienne_commande_id': cancelled.id,
            'commande': CommandeSerializer(commande).data,
            'stock_restant': available,
            'message_reprise': outbound.get('content'),
            'message_recap': recap.get('content'),
            'message_stock': offer.get('content'),
        }

    from .order_messaging import send_waiting_with_info_message

    wait = send_waiting_with_info_message(commande)
    return {
        'status': "Reprise — en liste d'attente",
        'reprise': True,
        'complet': False,
        'en_attente': True,
        'attente_confirmation': True,
        'ancienne_commande_id': cancelled.id,
        'commande': CommandeSerializer(commande).data,
        'message_reprise': outbound.get('content'),
        'message_recap': recap.get('content'),
        'message_attente': wait.get('content'),
    }


FIELD_UPDATE_PATTERNS = [
    (
        'telephone',
        re.compile(
            r'(?:hanova|ovaina|ovay|ova|soloina|soloy|modifi(?:er|e)|chang(?:er|e)|corrige[rz]?)\s+'
            r'(?:ny\s+)?(?:num[eé]ro|finday|tel(?:éphone)?|phone)\s*[:\-]?\s*(.+)$',
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        'adresse',
        re.compile(
            r'(?:hanova|ovaina|ovay|ova|soloina|soloy|modifi(?:er|e)|chang(?:er|e)|corrige[rz]?)\s+'
            r'(?:ny\s+)?(?:adresse|adiresy)\s*[:\-]?\s*(.+)$',
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        'date_livraison',
        re.compile(
            r'(?:hanova|ovaina|ovay|ova|soloina|soloy|modifi(?:er|e)|chang(?:er|e)|corrige[rz]?)\s+'
            r'(?:ny\s+)?(?:daty|date)\s*[:\-]?\s*(.+)$',
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        'heure_livraison',
        re.compile(
            r'(?:hanova|ovaina|ovay|ova|soloina|soloy|modifi(?:er|e)|chang(?:er|e)|corrige[rz]?)\s+'
            r'(?:ny\s+)?(?:ora|heure|time)\s*[:\-]?\s*(.+)$',
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        'nom',
        re.compile(
            r'(?:hanova|ovaina|ovay|ova|soloina|soloy|modifi(?:er|e)|chang(?:er|e)|corrige[rz]?)\s+'
            r'(?:ny\s+)?(?:anarana|nom)\s*[:\-]?\s*(.+)$',
            re.IGNORECASE | re.DOTALL,
        ),
    ),
]


def _extract_modification_fields(text: str) -> dict[str, str]:
    """Extrait « hanova adresse Ivato » / « ovaina ny numéro 034… »."""
    cleaned = (text or '').strip()
    if not cleaned:
        return {}
    parsed: dict[str, str] = {}
    for field, pattern in FIELD_UPDATE_PATTERNS:
        match = pattern.search(cleaned)
        if match:
            value = match.group(1).strip().split('\n')[0].strip()
            if value:
                parsed[field] = value
    # Quantité : « hanova firy 3 » / « ovaina ny isa 2 »
    qty_match = re.search(
        r'(?:hanova|ovaina|ovay|ova|soloina|modifi(?:er|e)|chang(?:er|e))\s+'
        r'(?:ny\s+)?(?:firy|isa|quantit[eé]|qte)\s*[:\-]?\s*(\d{1,3})\b',
        cleaned,
        re.IGNORECASE,
    )
    if qty_match:
        parsed['_quantite'] = qty_match.group(1)
    return parsed


QUANTITY_LABELLED_PATTERN = re.compile(
    r'(?:quantit[eé]|qte|qty|nombre|isan?[\'’y]?|isa)\s*[:=\-]?\s*(\d{1,3})',
    re.IGNORECASE,
)
QUANTITY_SUFFIX_PATTERN = re.compile(
    r'(\d{1,3})\s*(?:pcs?|pi[eè]ces?|unit[eé]s?|isa)\b',
    re.IGNORECASE,
)
QUANTITY_X_PATTERN = re.compile(r'(?:^|\s)x\s*(\d{1,3})\b|\b(\d{1,3})\s*x(?:\s|$)', re.IGNORECASE)
QUANTITY_STANDALONE_PATTERN = re.compile(r'^\s*(\d{1,3})\s*$')


def _parse_quantity(text: str, *, expecting: bool = False) -> int | None:
    """Extrait une quantité d'un message libre.

    Motifs explicites toujours acceptés : « quantité: 2 », « isa 2 », « 2 pcs », « x2 ».
    Un nombre seul (« 2 ») n'est interprété comme quantité que lorsqu'on l'attend
    (expecting=True), pour ne pas confondre avec un téléphone, une date ou une heure.
    """
    if not text:
        return None

    for pattern in (QUANTITY_LABELLED_PATTERN, QUANTITY_SUFFIX_PATTERN, ISA_QUANTITY_PATTERN):
        match = pattern.search(text)
        if match and int(match.group(1)) > 0:
            return int(match.group(1))

    match = QUANTITY_X_PATTERN.search(text)
    if match:
        value = int(match.group(1) or match.group(2))
        if value > 0:
            return value

    if expecting:
        for line in (l.strip() for l in text.splitlines() if l.strip()):
            if _looks_like_phone(line) or _looks_like_time(line) or _looks_like_date(line):
                continue
            standalone = QUANTITY_STANDALONE_PATTERN.match(line)
            if standalone:
                value = int(standalone.group(1))
                if 0 < value <= 999:
                    return value
    return None


def _is_quantity_line(line: str) -> bool:
    """Vrai si la ligne ne porte qu'une quantité (« 2 », « 2 pcs », « quantité : 2 »).

    Sert à éviter qu'un nombre seul soit pris à tort pour un nom ou une adresse.
    """
    cleaned = (line or '').strip()
    if not cleaned:
        return False
    if _looks_like_phone(cleaned) or _looks_like_time(cleaned) or _looks_like_date(cleaned):
        return False
    return bool(
        QUANTITY_STANDALONE_PATTERN.match(cleaned)
        or QUANTITY_LABELLED_PATTERN.search(cleaned)
        or QUANTITY_SUFFIX_PATTERN.search(cleaned)
        or ISA_QUANTITY_PATTERN.search(cleaned)
    )


def _available_stock_for_commande(commande: Commande) -> int:
    """Stock réellement disponible pour cette commande (après les JP devant elle)."""
    variante = commande._get_stock_variante()
    if not variante:
        return 10**9
    remaining = max(0, int(variante.stock))
    ahead = Commande.objects.filter(
        produit=commande.produit,
        variante=commande.variante,
        statut=Commande.STATUT_JP_CAPTURE,
        ordre_jp__lt=commande.ordre_jp,
    ).exclude(pk=commande.pk)
    qty_ahead = sum(c.quantite_effective for c in ahead)
    return max(0, remaining - qty_ahead)


def _order_is_eligible(commande: Commande) -> bool:
    """Vrai si la commande peut être confirmée maintenant (assez de stock, à son tour).

    Le stock courant de la variante reflète déjà les commandes confirmées (décrémentées).
    On ne compte donc que les JP encore en attente PLACÉS DEVANT (ordre_jp plus petit) :
    s'ils consomment déjà tout le stock, ce client reste en liste d'attente.
    """
    return _available_stock_for_commande(commande) >= commande.quantite_effective


ACCEPT_PARTIAL_PATTERNS = [
    re.compile(r'\b(?:oui|ok|oka|eken[ao]?|eka|prend|prends|prendre|alaina|alaiko|tonga)\b', re.I),
    re.compile(r'\b(?:izay\s+sisa|ny\s+sisa|reste|leftover)\b', re.I),
]
WAIT_STOCK_PATTERNS = [
    re.compile(r'\b(?:miandry|attendre|wait|non|tsia|tsy|plus\s+tard)\b', re.I),
]


def _looks_like_accept_partial(text: str, available: int) -> bool:
    cleaned = (text or '').strip()
    if not cleaned or available <= 0:
        return False
    if any(p.search(cleaned) for p in WAIT_STOCK_PATTERNS) and not any(
        p.search(cleaned) for p in ACCEPT_PARTIAL_PATTERNS
    ):
        return False
    qty = _parse_quantity(cleaned, expecting=True)
    if qty is not None and qty == available:
        return True
    if qty is not None and 0 < qty < available and any(p.search(cleaned) for p in ACCEPT_PARTIAL_PATTERNS):
        return True
    return any(p.search(cleaned) for p in ACCEPT_PARTIAL_PATTERNS)


def _looks_like_prefer_wait(text: str) -> bool:
    cleaned = (text or '').strip()
    if not cleaned:
        return False
    return any(p.search(cleaned) for p in WAIT_STOCK_PATTERNS)


def _looks_like_thanks(text: str) -> bool:
    """Vrai si le message est essentiellement un remerciement (pas une fiche infos)."""
    cleaned = (text or '').strip()
    if not cleaned or len(cleaned) > 120:
        return False
    normalized = _normalize_text(cleaned)
    if not (THANKS_PATTERN.search(cleaned) or THANKS_PATTERN.search(normalized)):
        return False
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if len(lines) >= 3:
        return False
    if _looks_like_phone(cleaned) or _parse_french_date(cleaned) or _parse_delivery_time(cleaned):
        return False
    if _parse_quantity(cleaned, expecting=False):
        return False
    return True


def _parsed_has_updatable_fields(parsed_data: dict[str, str]) -> bool:
    """Champs assez sûrs pour une MAJ après confirmation (pas un faux parse annulation)."""
    if parsed_data.get('telephone') and _looks_like_phone(str(parsed_data['telephone'])):
        return True
    if parsed_data.get('date_livraison') and _looks_like_date(str(parsed_data['date_livraison'])):
        return True
    if parsed_data.get('heure_livraison') and _looks_like_time(str(parsed_data['heure_livraison'])):
        return True
    adresse = (parsed_data.get('adresse') or '').strip()
    if not adresse:
        return False
    if _is_cancellation(adresse) or len(adresse) < 4:
        return False
    # Évite « alaiko ndray leizy azafady » pris pour une adresse.
    if re.search(
        r'\b(?:tsy|ala?iko|ndray|indray|leizy|ilay|azafady|bonjour|salama|tsy\s+ala)\b',
        adresse,
        re.IGNORECASE,
    ) and not re.search(r'\d', adresse):
        return False
    return True


def _ensure_paiement(commande: Commande) -> Paiement:
    """Crée le règlement par défaut (paiement à la livraison, non payé) si absent."""
    paiement, _ = Paiement.objects.get_or_create(
        commande=commande,
        defaults={
            'methode': Paiement.METHODE_LIVRAISON,
            'statut': Paiement.STATUT_NON_PAYE,
        },
    )
    return paiement


_CLIENT_PLACEHOLDER_NAMES = frozenset({'Client Live', 'Client Facebook', 'Client TikTok'})

_ORDER_THREAD_FIELD_KEYS = ('nom', 'telephone', 'adresse', 'date_livraison', 'heure_livraison')


def _client_fields_as_collected(client: Client) -> dict[str, str]:
    collected: dict[str, str] = {}
    if client.nom and client.nom not in _CLIENT_PLACEHOLDER_NAMES:
        collected['nom'] = client.nom
    if client.telephone:
        collected['telephone'] = client.telephone
    if client.adresse:
        collected['adresse'] = client.adresse
    if client.date_livraison_preferee:
        collected['date_livraison'] = client.date_livraison_preferee.strftime('%d/%m/%Y')
    if client.heure_livraison_preferee:
        collected['heure_livraison'] = client.heure_livraison_preferee.strftime('%H:%M')
    return collected


def _merge_collected_fields(*sources: dict[str, str]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for source in sources:
        for key, value in source.items():
            if value and not merged.get(key):
                merged[key] = value
    return merged


def _cancelled_predecessor_for_reprise(commande: Commande) -> Commande | None:
    """Commande annulée récente dont on reprend les infos (même client / déclinaison)."""
    from django.conf import settings

    if not commande.pk:
        return None
    max_hours = getattr(settings, 'AZLIVE_REPRISE_INFO_MAX_HOURS', 72)
    cutoff = timezone.now() - timedelta(hours=max_hours)
    predecessor = (
        Commande.objects.filter(
            client=commande.client,
            produit=commande.produit,
            variante=commande.variante,
            statut=Commande.STATUT_ANNULE,
            date_creation__lt=commande.date_creation,
            date_creation__gte=cutoff,
        )
        .order_by('-date_creation')
        .first()
    )
    if predecessor and _predecessor_had_complete_infos(predecessor):
        return predecessor
    return None


def _predecessor_had_complete_infos(cancelled: Commande) -> bool:
    """La commande annulée avait déjà toutes les infos dans son fil MP."""
    thread = _fields_from_commande_inbounds(cancelled)
    has_nom = bool(thread.get('nom')) or (
        cancelled.client.nom and cancelled.client.nom not in _CLIENT_PLACEHOLDER_NAMES
    )
    return bool(
        has_nom
        and thread.get('telephone')
        and thread.get('adresse')
        and thread.get('date_livraison')
        and thread.get('heure_livraison')
        and cancelled.quantite is not None
    )


def _is_reprise_commande(commande: Commande) -> bool:
    return _cancelled_predecessor_for_reprise(commande) is not None


def _effective_collected_fields(commande: Commande) -> dict[str, str]:
    """Champs utilisables pour cette commande (fil MP + héritage reprise récente)."""
    collected = _fields_from_commande_inbounds(commande)
    if not _is_reprise_commande(commande):
        return collected
    predecessor = _cancelled_predecessor_for_reprise(commande)
    inherited_thread = _fields_from_commande_inbounds(predecessor) if predecessor else {}
    return _merge_collected_fields(inherited_thread, collected)


def _has_reprise_confirmation_ack(commande: Commande) -> bool:
    for msg in commande.messages.filter(direction=Message.DIRECTION_INBOUND).order_by('date_envoi', 'id'):
        if _looks_like_confirmation_ack(msg.contenu):
            return True
    return False


def _needs_reprise_confirmation(commande: Commande) -> bool:
    """Reprise avec infos complètes : le client doit encore valider (eka / ok)."""
    return (
        _is_reprise_commande(commande)
        and not _missing_confirmation_fields(commande)
        and not _has_reprise_confirmation_ack(commande)
    )


def _reprise_has_info_update(parsed_data: dict[str, str], inbound_text: str) -> bool:
    if _looks_like_modification(inbound_text):
        return True
    return _parsed_has_updatable_fields(parsed_data)


class _ThreadPartialClient:

    def __init__(self, base: Client):
        self.nom = (
            base.nom if base.nom and base.nom not in _CLIENT_PLACEHOLDER_NAMES else ''
        )
        self.telephone = ''
        self.adresse = ''
        self.date_livraison_preferee = None
        self.heure_livraison_preferee = None


def _apply_thread_field(partial: _ThreadPartialClient, key: str, value: str) -> None:
    if key == 'nom':
        partial.nom = value
    elif key == 'telephone':
        partial.telephone = _normalize_phone(value) or value
    elif key == 'adresse':
        partial.adresse = value
    elif key == 'date_livraison':
        delivery_date = _parse_delivery_date(value)
        if delivery_date:
            partial.date_livraison_preferee = delivery_date
    elif key == 'heure_livraison':
        delivery_time = _parse_delivery_time(value)
        if delivery_time:
            partial.heure_livraison_preferee = delivery_time


def _fields_from_commande_inbounds(commande: Commande) -> dict[str, str]:
    """Champs explicitement fournis dans les MP entrants de cette commande."""
    merged: dict[str, str] = {}
    partial = _ThreadPartialClient(commande.client)
    for msg in commande.messages.filter(direction=Message.DIRECTION_INBOUND).order_by('date_envoi', 'id'):
        parsed = analyze_confirmation_message(msg.contenu, client=partial)
        for key in _ORDER_THREAD_FIELD_KEYS:
            if parsed.get(key):
                merged[key] = parsed[key]
                _apply_thread_field(partial, key, parsed[key])
    return merged


def _collected_fields_snapshot(commande: Commande) -> dict[str, Any]:
    """Résumé des infos reçues pour CETTE commande (héritage reprise inclus)."""
    collected = _effective_collected_fields(commande)
    client = commande.client
    snapshot: dict[str, Any] = {}
    nom = collected.get('nom') or (
        client.nom if client.nom and client.nom not in _CLIENT_PLACEHOLDER_NAMES else None
    )
    if nom:
        snapshot['nom'] = nom
    for key in ('telephone', 'adresse', 'date_livraison', 'heure_livraison'):
        if collected.get(key):
            snapshot[key] = collected[key]
    if commande.quantite is not None:
        snapshot['quantite'] = commande.quantite
    return snapshot


def _missing_confirmation_fields(commande: Commande) -> list[str]:
    """
    Champs encore requis pour confirmer cette commande.
    Nouveau JP : téléphone, adresse, date et heure doivent être dans le fil MP.
    Reprise après annulation : on réutilise les infos de la commande annulée.
    """
    collected = _effective_collected_fields(commande)
    client = commande.client
    missing = []
    has_nom = bool(collected.get('nom')) or (
        client.nom and client.nom not in _CLIENT_PLACEHOLDER_NAMES
    )
    if not has_nom:
        missing.append('nom')
    if not collected.get('telephone'):
        missing.append('telephone')
    if not collected.get('adresse'):
        missing.append('adresse')
    if not collected.get('date_livraison'):
        missing.append('date_livraison')
    if not collected.get('heure_livraison'):
        missing.append('heure_livraison')
    if commande.quantite is None:
        missing.append('quantite')
    return missing


def _client_snapshot(client: Client) -> dict[str, Any]:
    return {
        'nom': client.nom,
        'telephone': client.telephone,
        'adresse': client.adresse,
        'date_livraison_preferee': client.date_livraison_preferee,
        'heure_livraison_preferee': client.heure_livraison_preferee.strftime('%H:%M')
        if client.heure_livraison_preferee
        else None,
    }


def _snapshot_for_storage(client: Client, quantite: int | None) -> dict[str, Any]:
    snap = _client_snapshot(client)
    delivery_date = snap.get('date_livraison_preferee')
    if delivery_date:
        snap['date_livraison_preferee'] = delivery_date.isoformat()
    return {'client': snap, 'quantite': quantite}


def _save_modification_snapshot(commande: Commande, client: Client, quantite: int | None) -> None:
    """Mémorise l'état avant la dernière modification (annulable)."""
    _clear_modification_snapshot(commande)
    Message.objects.create(
        commande=commande,
        contenu=json.dumps(_snapshot_for_storage(client, quantite)),
        numero_relance=SNAPSHOT_NUMERO_RELANCE,
        direction=Message.DIRECTION_OUTBOUND,
        canal=SNAPSHOT_CANAL,
    )


def _get_modification_snapshot(commande: Commande) -> dict[str, Any] | None:
    msg = (
        Message.objects.filter(
            commande=commande,
            numero_relance=SNAPSHOT_NUMERO_RELANCE,
            canal=SNAPSHOT_CANAL,
        )
        .order_by('-date_envoi', '-id')
        .first()
    )
    if not msg:
        return None
    try:
        return json.loads(msg.contenu)
    except (json.JSONDecodeError, TypeError):
        return None


def _clear_modification_snapshot(commande: Commande) -> None:
    Message.objects.filter(
        commande=commande,
        numero_relance=SNAPSHOT_NUMERO_RELANCE,
        canal=SNAPSHOT_CANAL,
    ).delete()


def _restore_client_from_snapshot(client: Client, snapshot_client: dict[str, Any]) -> None:
    client.nom = snapshot_client.get('nom') or client.nom
    client.telephone = snapshot_client.get('telephone') or ''
    client.adresse = snapshot_client.get('adresse') or ''
    raw_date = snapshot_client.get('date_livraison_preferee')
    if raw_date:
        client.date_livraison_preferee = (
            date.fromisoformat(raw_date) if isinstance(raw_date, str) else raw_date
        )
    else:
        client.date_livraison_preferee = None
    raw_time = snapshot_client.get('heure_livraison_preferee')
    if raw_time:
        client.heure_livraison_preferee = _parse_delivery_time(str(raw_time))
    else:
        client.heure_livraison_preferee = None


def _revert_last_modification(commande: Commande, client: Client) -> dict[str, Any]:
    """Annule la dernière modification et restaure l'état précédent."""
    snapshot = _get_modification_snapshot(commande)
    if not snapshot:
        from .order_messaging import send_modification_revert_unavailable_message

        outbound = send_modification_revert_unavailable_message(commande)
        return {
            'status': 'Aucune modification à annuler',
            'modification_annulee': False,
            'complet': commande.statut == Commande.STATUT_CONFIRME,
            'commande': CommandeSerializer(commande).data,
            'client': _client_snapshot(client),
            'message_modification': outbound.get('content'),
            'message_delivery': outbound.get('delivery'),
        }

    snapshot_client = snapshot.get('client') or {}
    snapshot_qty = snapshot.get('quantite')
    current_qty = commande.quantite

    if (
        commande.statut == Commande.STATUT_CONFIRME
        and snapshot_qty is not None
        and current_qty is not None
        and snapshot_qty != current_qty
    ):
        commande._adjust_variante_stock(current_qty - snapshot_qty)

    _restore_client_from_snapshot(client, snapshot_client)
    client.save(
        update_fields=[
            'nom',
            'telephone',
            'adresse',
            'date_livraison_preferee',
            'heure_livraison_preferee',
        ],
    )

    if snapshot_qty is not None and commande.quantite != snapshot_qty:
        commande.quantite = snapshot_qty
        commande.save(update_fields=['quantite'])

    _clear_modification_snapshot(commande)

    from .order_messaging import send_modification_revert_message, send_reprise_recap_message

    if _is_reprise_commande(commande) and commande.statut == Commande.STATUT_JP_CAPTURE:
        recap = send_reprise_recap_message(commande)
        outbound = send_modification_revert_message(commande, reprise=True)
        return {
            'status': 'Modification annulée — confirmez vos informations',
            'modification_annulee': True,
            'complet': False,
            'attente_confirmation': True,
            'reprise': True,
            'champs_recus': _collected_fields_snapshot(commande),
            'commande': CommandeSerializer(commande).data,
            'client': _client_snapshot(client),
            'message_modification': outbound.get('content'),
            'message_recap': recap.get('content'),
            'message_delivery': recap.get('delivery'),
        }

    outbound = send_modification_revert_message(commande, reprise=False)
    return {
        'status': 'Modification annulée',
        'modification_annulee': True,
        'complet': commande.statut == Commande.STATUT_CONFIRME,
        'commande': CommandeSerializer(commande).data,
        'client': _client_snapshot(client),
        'message_modification': outbound.get('content'),
        'message_delivery': outbound.get('delivery'),
    }


def _apply_parsed_fields(client: Client, parsed_data: dict[str, str]) -> None:
    if parsed_data.get('nom'):
        client.nom = parsed_data['nom']
    if parsed_data.get('telephone'):
        client.telephone = _normalize_phone(parsed_data['telephone']) or parsed_data['telephone']
    if parsed_data.get('adresse'):
        client.adresse = parsed_data['adresse']
    delivery_date = _parse_delivery_date(parsed_data.get('date_livraison'))
    if delivery_date:
        client.date_livraison_preferee = delivery_date
    delivery_time = _parse_delivery_time(parsed_data.get('heure_livraison'))
    if delivery_time:
        client.heure_livraison_preferee = delivery_time


def analyze_confirmation_message(text: str, client: Client | None = None) -> dict[str, str]:
    from .ai import ConfirmationMessageAnalyzer

    return ConfirmationMessageAnalyzer().analyze(text, client=client)['fields']


@transaction.atomic
def handle_client_reply(
    commande: Commande,
    parsed_data: dict[str, str],
    *,
    inbound_text: str = '',
    canal: str | None = None,
) -> dict[str, Any]:
    """Enregistre ce que le client a envoyé ; confirme si complet, sinon demande le reste."""
    client = commande.client
    canal_message = canal or detect_client_channel(client)

    if inbound_text:
        Message.objects.create(
            commande=commande,
            contenu=inbound_text,
            numero_relance=0,
            direction=Message.DIRECTION_INBOUND,
            canal=canal_message,
        )

    if _looks_like_thanks(inbound_text):
        from .order_messaging import send_thanks_ack_message

        outbound = send_thanks_ack_message(commande=commande, client=client)
        return {
            'status': 'Remerciement pris en compte',
            'complet': commande.statut == Commande.STATUT_CONFIRME,
            'remerciement': True,
            'commande': CommandeSerializer(commande).data,
            'client': _client_snapshot(client),
            'message_remerciement_ack': outbound.get('content'),
            'message_delivery': outbound.get('delivery'),
        }

    if _is_modification_cancellation(inbound_text):
        return _revert_last_modification(commande, client)

    if _is_cancellation(inbound_text):
        if commande.statut not in CANCELLABLE_STATUSES:
            raise OrderConfirmationError(
                f'La commande #{commande.id} ne peut plus être annulée '
                f'(statut : {commande.get_statut_display()}).',
                status_code=409,
            )

        commande.statut = Commande.STATUT_ANNULE
        commande.save(update_fields=['statut'])
        _clear_modification_snapshot(commande)

        from .order_messaging import send_order_cancelled_message

        outbound = send_order_cancelled_message(commande)
        return {
            'status': 'Commande annulée',
            'annule': True,
            'complet': False,
            'commande': CommandeSerializer(commande).data,
            'client': _client_snapshot(client),
            'message_annulation': outbound.get('content'),
            'message_delivery': outbound.get('delivery'),
        }

    # Modification après confirmation / préparation (adresse, tél, daty, qté…).
    if commande.statut in (Commande.STATUT_CONFIRME, Commande.STATUT_PREPARE):
        mod_fields = _extract_modification_fields(inbound_text)
        if not parsed_data and inbound_text:
            parsed_data = analyze_confirmation_message(inbound_text, client=client)
        # Les extractions « hanova … » priment sur le parse libre.
        for key, value in mod_fields.items():
            if key != '_quantite':
                parsed_data[key] = value
        new_qty = None
        if mod_fields.get('_quantite'):
            new_qty = int(mod_fields['_quantite'])
        else:
            new_qty = _parse_quantity(
                inbound_text,
                expecting=_looks_like_modification(inbound_text),
            )
        wants_mod = _looks_like_modification(inbound_text) or _parsed_has_updatable_fields(parsed_data)
        if wants_mod or (new_qty and _looks_like_modification(inbound_text)):
            changed = []
            before = _client_snapshot(client)
            old_qty = commande.quantite_effective
            _save_modification_snapshot(commande, client, commande.quantite)
            # Sans « hanova … », ne pas écraser le nom avec un parse hasardeux (ex. « tsy »).
            if not _looks_like_modification(inbound_text) and not mod_fields.get('nom'):
                parsed_data.pop('nom', None)
            # Ne pas écraser le nom avec toute la phrase « hanova adresse … ».
            if parsed_data.get('nom') and _looks_like_modification(parsed_data['nom']):
                parsed_data.pop('nom', None)
            _apply_parsed_fields(client, parsed_data)
            client.save(
                update_fields=[
                    'nom',
                    'telephone',
                    'adresse',
                    'date_livraison_preferee',
                    'heure_livraison_preferee',
                ],
            )
            after = _client_snapshot(client)
            for key, label in (
                ('nom', 'anarana'),
                ('telephone', 'numéro'),
                ('adresse', 'adresse'),
                ('date_livraison_preferee', 'daty'),
                ('heure_livraison_preferee', 'ora'),
            ):
                if before.get(key) != after.get(key) and after.get(key):
                    changed.append(label)
            if new_qty and new_qty != old_qty:
                delta = old_qty - new_qty
                commande._adjust_variante_stock(delta)
                commande.quantite = new_qty
                commande.save(update_fields=['quantite'])
                changed.append(f'firy ({new_qty})')
            if not changed:
                from .order_messaging import send_modification_ack_message

                outbound = send_modification_ack_message(
                    commande,
                    [],
                    prompt_details=True,
                )
                return {
                    'status': 'Modification demandée — précisez le champ',
                    'modifie': False,
                    'complet': True,
                    'commande': CommandeSerializer(commande).data,
                    'client': _client_snapshot(client),
                    'message_modification': outbound.get('content'),
                    'message_delivery': outbound.get('delivery'),
                }
            from .order_messaging import send_modification_ack_message

            outbound = send_modification_ack_message(commande, changed)
            return {
                'status': 'Commande mise à jour',
                'modifie': True,
                'complet': True,
                'champs_modifies': changed,
                'commande': CommandeSerializer(commande).data,
                'client': _client_snapshot(client),
                'message_modification': outbound.get('content'),
                'message_delivery': outbound.get('delivery'),
            }
        raise OrderConfirmationError(
            f'La commande #{commande.id} est déjà au statut {commande.get_statut_display()}. '
            f'Raha te-hanova ianao, soraty ohatra : « hanova adresse … » na « ovaina ny numéro … ».',
            status_code=409,
        )

    # Au-delà de l'annulation, la complétion d'infos ne concerne que les JP en attente.
    if commande.statut != Commande.STATUT_JP_CAPTURE:
        raise OrderConfirmationError(
            f'La commande #{commande.id} est déjà au statut {commande.get_statut_display()}.',
            status_code=409,
        )

    if _looks_like_confirmation_ack(inbound_text) and not _reprise_has_info_update(parsed_data, inbound_text):
        parsed_data = {}
    else:
        will_update = (
            not _looks_like_confirmation_ack(inbound_text)
            and _reprise_has_info_update(parsed_data, inbound_text)
        )
        is_reprise_pending = (
            commande.statut == Commande.STATUT_JP_CAPTURE and _is_reprise_commande(commande)
        )
        if will_update and is_reprise_pending:
            _save_modification_snapshot(commande, client, commande.quantite)
        _apply_parsed_fields(client, parsed_data)

    client.save(
        update_fields=[
            'nom',
            'telephone',
            'adresse',
            'date_livraison_preferee',
            'heure_livraison_preferee',
        ],
    )

    # Quantité : demandée pendant la collecte (pas dans le JP). On n'accepte un nombre
    # « nu » que tant qu'on attend justement la quantité.
    if commande.quantite is None:
        quantite = _parse_quantity(inbound_text, expecting=True)
        if quantite:
            commande.quantite = quantite
            commande.save(update_fields=['quantite'])

    missing = _missing_confirmation_fields(commande)
    if missing:
        from .order_messaging import send_completion_request_message

        outbound = send_completion_request_message(commande, missing)
        return {
            'status': 'Informations partielles — complétez quand vous voulez',
            'complet': False,
            'champs_manquants': missing,
            'champs_recus': _collected_fields_snapshot(commande),
            'parsed': parsed_data,
            'client': _client_snapshot(client),
            'message_relance': outbound.get('content'),
            'message_delivery': outbound.get('delivery'),
        }

    if _needs_reprise_confirmation(commande):
        ack = _looks_like_confirmation_ack(inbound_text)
        updated = _reprise_has_info_update(parsed_data, inbound_text)
        if not ack:
            from .order_messaging import send_reprise_recap_message

            outbound = send_reprise_recap_message(commande)
            status = (
                'Modification enregistrée — confirmez vos informations'
                if updated
                else 'En attente de confirmation'
            )
            return {
                'status': status,
                'complet': False,
                'attente_confirmation': True,
                'reprise': True,
                'modification_en_attente': bool(updated),
                'champs_recus': _collected_fields_snapshot(commande),
                'commande': CommandeSerializer(commande).data,
                'client': _client_snapshot(client),
                'message_recap': outbound.get('content'),
                'message_delivery': outbound.get('delivery'),
            }

    available = _available_stock_for_commande(commande)
    requested = commande.quantite_effective

    # Client a déjà reçu l'offre « il reste X » : il accepte X ou préfère attendre.
    if available > 0 and requested > available:
        if _looks_like_accept_partial(inbound_text, available):
            accepted_qty = _parse_quantity(inbound_text, expecting=True)
            if accepted_qty is None or accepted_qty > available:
                accepted_qty = available
            commande.quantite = accepted_qty
            commande.save(update_fields=['quantite'])
            if _order_is_eligible(commande):
                return _finalize_confirmation(commande, parsed_data=parsed_data)
        elif _looks_like_prefer_wait(inbound_text):
            from .order_messaging import send_waiting_with_info_message

            outbound = send_waiting_with_info_message(commande)
            return {
                'status': "En liste d'attente — client préfère attendre",
                'complet': False,
                'en_attente': True,
                'stock_restant': available,
                'quantite_demandee': requested,
                'commande': CommandeSerializer(commande).data,
                'client': _client_snapshot(client),
                'message_attente': outbound.get('content'),
                'message_delivery': outbound.get('delivery'),
            }
        else:
            from .order_messaging import send_stock_partial_offer_message

            outbound = send_stock_partial_offer_message(commande, available)
            return {
                'status': 'Stock insuffisant — offre du reste proposée',
                'complet': False,
                'en_attente': True,
                'stock_restant': available,
                'quantite_demandee': requested,
                'commande': CommandeSerializer(commande).data,
                'client': _client_snapshot(client),
                'message_stock': outbound.get('content'),
                'message_delivery': outbound.get('delivery'),
            }

    # Infos complètes, mais file d'attente (plus de stock ou personnes devant).
    if not _order_is_eligible(commande):
        from .order_messaging import send_waiting_with_info_message

        outbound = send_waiting_with_info_message(commande)
        return {
            'status': "En liste d'attente — informations enregistrées",
            'complet': False,
            'en_attente': True,
            'stock_restant': available,
            'champs_manquants': [],
            'commande': CommandeSerializer(commande).data,
            'client': _client_snapshot(client),
            'parsed': parsed_data,
            'message_attente': outbound.get('content'),
            'message_delivery': outbound.get('delivery'),
        }

    return _finalize_confirmation(commande, parsed_data=parsed_data)


def _finalize_confirmation(
    commande: Commande,
    *,
    parsed_data: dict[str, str] | None = None,
    promoted: bool = False,
) -> dict[str, Any]:
    """Confirme la commande : statut CONFIRME (décrément stock via save) + règlement + message.

    promoted=True quand la confirmation vient d'une montée en file (une place s'est libérée
    et les informations du client étaient déjà complètes) : le message le signale.
    """
    commande.statut = Commande.STATUT_CONFIRME
    commande.save(update_fields=['statut'])
    _clear_modification_snapshot(commande)
    paiement = _ensure_paiement(commande)

    from .order_messaging import send_order_confirmed_message

    outbound = send_order_confirmed_message(commande, promoted=promoted)

    return {
        'status': 'Commande confirmée',
        'complet': True,
        'commande': CommandeSerializer(commande).data,
        'reglement': {'methode': paiement.methode, 'statut': paiement.statut},
        'client': _client_snapshot(commande.client),
        'parsed': parsed_data or {},
        'message_remerciement': outbound.get('content'),
        'message_delivery': outbound.get('delivery'),
        'facture_url': outbound.get('facture_url'),
        'etiquette_url': outbound.get('etiquette_url'),
    }


@transaction.atomic
def expire_commande(commande: Commande) -> dict[str, Any] | None:
    """Expire un JP en tête de file resté incomplet trop longtemps.

    On annule la commande (ce qui, via Commande.save(), fait monter le suivant de la file —
    confirmé automatiquement s'il est déjà complet) puis on prévient le client expiré.
    """
    if commande.statut != Commande.STATUT_JP_CAPTURE:
        return None

    commande.statut = Commande.STATUT_ANNULE
    commande.save(update_fields=['statut'])

    from .order_messaging import send_order_expired_message

    outbound = send_order_expired_message(commande)
    return {
        'commande_id': commande.id,
        'message_expiration': outbound.get('content'),
        'message_delivery': outbound.get('delivery'),
    }


def promote_queue(produit, variante=None, exclude_pk=None) -> None:
    """Fait avancer la file d'attente d'une déclinaison après libération de stock/place.

    Confirme automatiquement les commandes suivantes qui sont à la fois ÉLIGIBLES (stock)
    et COMPLÈTES (toutes les infos + quantité fournies). Dès qu'on rencontre une commande
    éligible mais incomplète, on lui demande ce qui manque et on s'arrête (elle garde sa
    place tant qu'elle n'a pas répondu).

    Si le stock restant est > 0 mais insuffisant pour la quantité demandée, on propose
    de prendre le reste (le client répond oui / miandry) et on s'arrête.
    """
    while True:
        queryset = (
            Commande.objects.select_related('client', 'produit', 'variante')
            .filter(produit=produit, variante=variante, statut=Commande.STATUT_JP_CAPTURE)
            .order_by('ordre_jp')
        )
        if exclude_pk:
            queryset = queryset.exclude(pk=exclude_pk)

        commande = queryset.first()
        if commande is None:
            return

        available = _available_stock_for_commande(commande)
        if available <= 0:
            return

        missing = _missing_confirmation_fields(commande)
        if missing:
            # Place libérée mais infos incomplètes : on prévient et on demande ce qui manque.
            from .order_messaging import send_promotion_completion_message

            send_promotion_completion_message(commande, missing)
            return

        requested = commande.quantite_effective
        if requested > available:
            from .order_messaging import send_stock_partial_offer_message

            send_stock_partial_offer_message(commande, available)
            return

        # TikTok (formulaire public) : ne pas auto-confirmer.
        # Le client rouvre /commander/<live> et clique sur Confirmer.
        client = commande.client
        if client.tiktok_id and not client.facebook_id and commande.live_id:
            from .order_messaging import send_public_form_spot_available_message

            send_public_form_spot_available_message(commande)
            return

        # Messenger / autres canaux : confirmation automatique si infos déjà complètes.
        _finalize_confirmation(commande, promoted=True)


@transaction.atomic
def confirm_commande_from_message(
    commande: Commande,
    parsed_data: dict[str, str],
    *,
    inbound_text: str = '',
    canal: str | None = None,
) -> dict[str, Any]:
    return handle_client_reply(
        commande,
        parsed_data,
        inbound_text=inbound_text,
        canal=canal,
    )


def process_inbound_private_message(
    *,
    sender_id: str,
    message_text: str,
    channel: str,
    page_id: str | None = None,
    id_field: str = 'facebook_id',
    referral_ref: str = '',
) -> dict[str, Any]:
    if not sender_id:
        raise OrderConfirmationError('Message privé vide ou expéditeur manquant.')

    # Ouverture m.me?ref=jp_123 : rattache le PSID et rappelle les infos manquantes.
    if referral_ref.startswith('jp_'):
        try:
            commande_id = int(referral_ref.split('_', 1)[1])
        except (TypeError, ValueError):
            commande_id = None
        if commande_id:
            commande = (
                Commande.objects.select_related('client', 'produit', 'variante', 'live')
                .filter(pk=commande_id, statut=Commande.STATUT_JP_CAPTURE)
                .first()
            )
            if commande:
                client = commande.client
                if id_field == 'facebook_id' and (
                    not client.facebook_id
                    or str(client.facebook_id).startswith('fb_comment:')
                    or not str(client.facebook_id).isdigit()
                ):
                    if not Client.objects.filter(facebook_id=sender_id).exclude(pk=client.pk).exists():
                        client.facebook_id = str(sender_id)
                        client.save(update_fields=['facebook_id'])
                text = (message_text or '').strip()
                if not text or text.startswith('[ref:'):
                    from .order_messaging import send_completion_request_message

                    missing = _missing_confirmation_fields(commande)
                    if missing:
                        outbound = send_completion_request_message(commande, missing)
                        return {
                            'status': 'Fil Messenger ouvert — infos demandées',
                            'complet': False,
                            'champs_manquants': missing,
                            'commande_id': commande.id,
                            'message_client': outbound.get('content'),
                        }
                    # Infos déjà là : retraiter comme une confirmation.
                    message_text = (
                        f"{client.nom}\n{client.telephone}\n{client.adresse}\n"
                        f"{client.date_livraison_preferee}\n"
                        f"{client.heure_livraison_preferee.strftime('%H:%M') if client.heure_livraison_preferee else ''}\n"
                        f"{commande.quantite or ''}"
                    ).strip()

    if not message_text:
        raise OrderConfirmationError('Message privé vide ou expéditeur manquant.')

    lookup = {id_field: sender_id}
    client = Client.objects.filter(**lookup).first()

    vendeur = None
    if page_id:
        page = PageFacebook.objects.select_related('vendeur').filter(page_id=str(page_id)).first()
        vendeur = page.vendeur if page else None

    # JP capturé avec auteur Meta masqué (souvent admin) : rattache le PSID Messenger.
    if client is None and id_field == 'facebook_id':
        client = claim_masked_facebook_client(real_facebook_id=str(sender_id), vendeur=vendeur)
    if client is None and channel == 'Facebook':
        client = link_messenger_sender_to_client(sender_id, vendeur)
        if client:
            logger.info(
                'Webhook Messenger : client #%s relié au PSID %s',
                client.pk,
                sender_id,
            )

    from .human_assistance import (
        _looks_like_order_info,
        analyze_client_message,
        handle_human_assistance_request,
        is_off_topic_private_message,
        needs_human_assistance,
    )
    from .mp_intent import classify_private_message_intent

    analysis = analyze_client_message(message_text, vendeur=vendeur)
    intent = classify_private_message_intent(message_text, client=client)
    intent_name = intent.get('intent', 'autre')

    if intent_name == 'remerciement':
        if not client:
            raise OrderConfirmationError(
                'Aucun client trouvé pour cet identifiant. Postez d\'abord un JP pendant le live.',
                status_code=404,
            )
        pending = find_pending_commande(client, vendeur=vendeur)
        recent = pending or (
            Commande.objects.filter(client=client)
            .order_by('-date_creation')
            .first()
        )
        if vendeur and recent and recent.produit.vendeur_id != vendeur.id:
            recent = (
                Commande.objects.filter(client=client, produit__vendeur=vendeur)
                .order_by('-date_creation')
                .first()
            )
        from .order_messaging import send_thanks_ack_message

        if recent:
            Message.objects.create(
                commande=recent,
                contenu=message_text,
                numero_relance=0,
                direction=Message.DIRECTION_INBOUND,
                canal=channel,
            )
            outbound = send_thanks_ack_message(commande=recent, client=client)
        else:
            outbound = send_thanks_ack_message(client=client)
        return {
            'status': 'Remerciement pris en compte',
            'remerciement': True,
            'complet': bool(recent and recent.statut == Commande.STATUT_CONFIRME),
            'message_remerciement_ack': outbound.get('content'),
            'message_delivery': outbound.get('delivery'),
        }

    if intent_name == 'reprise':
        if not client:
            raise OrderConfirmationError(
                'Aucun client trouvé pour cet identifiant. Postez d\'abord un JP pendant le live.',
                status_code=404,
            )
        cancelled = find_last_cancelled_commande(client, vendeur=vendeur)
        if not cancelled:
            raise OrderConfirmationError(
                'Aucune commande annulée à reprendre. Manaova JP indray azafady.',
                status_code=404,
            )
        Message.objects.create(
            commande=cancelled,
            contenu=message_text,
            numero_relance=0,
            direction=Message.DIRECTION_INBOUND,
            canal=channel,
        )
        return reopen_after_cancel(cancelled)

    if intent_name == 'annulation_modification':
        if not client:
            raise OrderConfirmationError(
                'Aucun client trouvé pour cet identifiant. Postez d\'abord un JP pendant le live.',
                status_code=404,
            )
        commande = find_pending_commande(client, vendeur=vendeur) or find_cancellable_commande(
            client, vendeur=vendeur
        )
        if not commande:
            raise OrderConfirmationError(
                'Aucune commande active pour annuler une modification.',
                status_code=404,
            )
        return handle_client_reply(
            commande,
            {},
            inbound_text=message_text,
            canal=channel,
        )

    if intent_name == 'annulation':
        if not client:
            raise OrderConfirmationError(
                'Aucun client trouvé pour cet identifiant. Postez d\'abord un JP pendant le live.',
                status_code=404,
            )
        commande = find_pending_commande(client, vendeur=vendeur) or find_cancellable_commande(
            client, vendeur=vendeur
        )
        if not commande:
            raise OrderConfirmationError(
                'Aucune commande active à annuler pour ce client.',
                status_code=404,
            )
        # Ne pas passer par l'escalade « hors sujet » : l'annulation est un intent métier.
        return handle_client_reply(
            commande,
            {},
            inbound_text=message_text,
            canal=channel,
        )

    if intent_name == 'modification':
        if not client:
            raise OrderConfirmationError(
                'Aucun client trouvé pour cet identifiant. Postez d\'abord un JP pendant le live.',
                status_code=404,
            )
        parsed = analyze_confirmation_message(message_text, client=client)
        commande = find_cancellable_commande(client, vendeur=vendeur)
        if commande and commande.statut in (
            Commande.STATUT_CONFIRME,
            Commande.STATUT_PREPARE,
        ):
            return handle_client_reply(
                commande,
                parsed,
                inbound_text=message_text,
                canal=channel,
            )
        commande = find_pending_commande(client, vendeur=vendeur)
        if commande:
            return handle_client_reply(
                commande,
                parsed,
                inbound_text=message_text,
                canal=channel,
            )
        raise OrderConfirmationError(
            'Aucune commande active à modifier pour ce client.',
            status_code=404,
        )

    if intent_name == 'infos_commande':
        if not client:
            raise OrderConfirmationError(
                'Aucun client trouvé pour cet identifiant. Postez d\'abord un JP pendant le live.',
                status_code=404,
            )
        parsed = analyze_confirmation_message(message_text, client=client)
        commande = find_pending_commande(client, vendeur=vendeur)
        if commande:
            return handle_client_reply(
                commande,
                parsed,
                inbound_text=message_text,
                canal=channel,
            )
        commande = find_cancellable_commande(client, vendeur=vendeur)
        if (
            commande
            and commande.statut in (Commande.STATUT_CONFIRME, Commande.STATUT_PREPARE)
            and _parsed_has_updatable_fields(parsed)
        ):
            return handle_client_reply(
                commande,
                parsed,
                inbound_text=message_text,
                canal=channel,
            )
        raise OrderConfirmationError(
            'Aucune commande JP en attente de confirmation pour ce client.',
            status_code=404,
        )

    if intent_name == 'question':
        if not client:
            raise OrderConfirmationError(
                'Aucun client trouvé pour cet identifiant. Postez d\'abord un JP pendant le live.',
                status_code=404,
            )
        return handle_human_assistance_request(
            client=client,
            message_text=message_text,
            channel=channel,
            vendeur=vendeur,
            page_id=page_id,
            analysis=analysis,
        )

    commande = find_pending_commande(client, vendeur=vendeur) if client else None
    if not commande:
        if needs_human_assistance(analysis) or is_off_topic_private_message(message_text):
            if not client:
                raise OrderConfirmationError(
                    'Aucun client trouvé pour cet identifiant. Postez d\'abord un JP pendant le live.',
                    status_code=404,
                )
            return handle_human_assistance_request(
                client=client,
                message_text=message_text,
                channel=channel,
                vendeur=vendeur,
                page_id=page_id,
                analysis=analysis,
            )
        raise OrderConfirmationError(
            'Aucune commande JP en attente de confirmation pour ce client.',
            status_code=404,
        )

    parsed = analyze_confirmation_message(message_text, client=client)

    # Infos de livraison : priorité absolue sur human_assistance (régression post-pull).
    if commande and (parsed or _looks_like_order_info(message_text, parsed)):
        return handle_client_reply(
            commande,
            parsed,
            inbound_text=message_text,
            canal=channel,
        )

    if not _looks_like_order_info(message_text, parsed) and (
        is_off_topic_private_message(message_text, parsed) or needs_human_assistance(analysis)
    ):
        return handle_human_assistance_request(
            client=client,
            message_text=message_text,
            channel=channel,
            vendeur=vendeur,
            page_id=page_id,
            commande=commande,
            analysis=analysis,
        )

    return handle_client_reply(
        commande,
        parsed,
        inbound_text=message_text,
        canal=channel,
    )
