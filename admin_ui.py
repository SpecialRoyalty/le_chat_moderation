"""Pure helpers for the Telegram owner-only, button-driven administration panel."""
from decimal import Decimal, InvalidOperation
from html import escape
from logic import parse_hm

PRESET_SLOTS = (
    ('22h – 00h', '22:00', '00:00'),
    ('00h – 02h', '00:00', '02:00'),
    ('23h – 01h', '23:00', '01:00'),
    ('00h – 03h', '00:00', '03:00'),
    ('15h – 17h', '15:00', '17:00'),
)

POST_INTERVALS = {
    'ad': (30, 120, 180, 360),
    'invite': (30, 120, 180, 360),
    'vip': (120, 180, 300, 600),
}

def parse_slot(value: str):
    """Accept 22:00 00:00, 22:00-00:00 or 22:00–00:00."""
    parts = value.replace('–', ' ').replace('—', ' ').replace('-', ' ').split()
    if len(parts) != 2:
        raise ValueError('Envoie deux heures au format HH:MM HH:MM.')
    for item in parts:
        parse_hm(item)
    if parts[0] == parts[1]:
        raise ValueError('Le début et la fin doivent être différents.')
    return parts[0], parts[1]

def parse_euros(value: str):
    try:
        price = Decimal(value.replace(',', '.').strip())
    except (InvalidOperation, ValueError):
        raise ValueError('Montant invalide, par exemple 35 ou 35,50.') from None
    if not price.is_finite() or price <= 0 or price > 100000 or price.as_tuple().exponent < -2:
        raise ValueError('Prix positif, maximum 100000 €, avec 2 décimales.')
    return price

def parse_user_id(value: str):
    value = value.strip()
    if not value.isdecimal() or not (0 < int(value) < 2**63):
        raise ValueError('Envoie un identifiant Telegram numérique positif.')
    return int(value)

def short(value: str, limit: int = 45):
    value = value.replace('\n', ' ').strip()
    return escape(value[:limit] + ('…' if len(value) > limit else ''))


def bool_label(value: bool):
    return '🟢 ON' if value else '🔴 OFF'
