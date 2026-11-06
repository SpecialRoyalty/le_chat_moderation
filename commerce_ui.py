"""Telegram native catalog, invitation and checkout screen formatting.

Telegram controls the theme. Inline button styles are hints, not arbitrary CSS.
"""
from decimal import Decimal, ROUND_HALF_UP
from html import escape


def money(value):
    """Format a decimal EUR amount (no float rounding)."""
    d = Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return f'{d:.2f}'.replace('.', ',').removesuffix(',00') + ' €'


def payment_total(value, method):
    amount = Decimal(str(value))
    if method not in ('crypto', 'paypal', 'revolut'):
        raise ValueError('Mode de paiement non pris en charge')
    return amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def mask_display(name, uid=None):
    cleaned = ' '.join((name or '').split())
    if not cleaned:
        return 'Membre ••••'
    first = cleaned.split()[0]
    if len(first) == 1:
        return first + '••••'
    return first[0] + '••••' + first[-1]


def vip_catalog_view(offers, selected, first_name='', lang='fr'):
    en = lang == 'en'
    header = (f'Hello {escape(first_name)}, available VIP offers:' if en
              else f'Salut {escape(first_name)}, les VIP disponibles :')
    lines = [header, '']
    labels = []
    for offer in offers:
        uid = int(offer['id'])
        title = str(offer['title'])
        description = str(offer['description'])
        goal=int(offer.get('invite_goal',0))
        price = money(offer['price_eur']) if offer['price_eur'] is not None else 'sur invitations'
        duration = ('lifetime' if en else 'à vie') if int(offer['days']) == 0 else (('24 hours' if en else '24 heures') if int(offer['days']) == 1 else (f'{offer["days"]} days' if en else f'{offer["days"]} jours'))
        access=f'{price} ou {goal} invitations' if goal and offer['price_eur'] is not None else (f'{goal} invitations' if goal else price)
        lines.append(f'👉 <b>{escape(title)}</b> {access} {duration}')
        if description:
            lines.append(escape(description[:180]))
        mark = '✅' if uid in selected else '▫️'
        if offer['price_eur'] is not None:
            labels.append((f'{mark} {title[:30]} {price}', uid))
    lines.extend(['', '👇 ' + ('Select your VIPs' if en else 'Choisis le/les VIP que tu veux') + ' 👇'])
    count = len(selected)
    continue_label = (f'Continuer ({count} VIP sélectionné' + ('s' if count != 1 else '') + ')') if not en else f'Continue ({count} selected)'
    return '\n'.join(lines), labels, continue_label


def vip_confirm_view(offers, lang='fr'):
    en = lang == 'en'
    lines = ['<b>' + ('Confirm your order:' if en else 'Es-tu sûr de vouloir commander :') + '</b>', '']
    for offer in offers:
        lines.append(f'🍒 {escape(str(offer["title"]))} : {money(offer["price_eur"])}')
    total = sum((Decimal(str(o['price_eur'])) for o in offers), Decimal('0'))
    lines.extend(['', f'<b>💰 Total : {money(total)}</b>'])
    return '\n'.join(lines), total


def payment_methods_view(total, lang='fr', methods=None):
    """Show only payment methods for which instructions are configured."""
    en = lang == 'en'
    names={'crypto':'Crypto','paypal':'PayPal','revolut':'Revolut'}
    chosen=tuple(names) if methods is None else tuple(m for m in methods if m in names)
    available=' · '.join(names[m] for m in chosen)
    return (f'<b>{"Choose your payment method:" if en else "Choisis ton mode de paiement :"}</b>\n\n'
            f'{"Order total" if en else "Total du panier"} : <b>{money(total)}</b>\n\n'
            f'{available}\n'
            + ('No additional bot fees.' if en else 'Aucun supplément appliqué par le bot.'))


def invite_dashboard_view(link, credited, pending, total, lang='fr'):
    en = lang == 'en'
    if en:
        return ('<b>🔗 MY INVITATIONS</b>\n\n'
                f'👥 Validated: <b>{credited}</b>\n'
                f'⏳ Pending: <b>{pending}</b>\n'
                f'📨 Total tracked: <b>{total}</b>\n\n'
                f'Your personal invite link:\n<code>{escape(link)}</code>')
    return ('<b>🔗 MES INVITATIONS</b>\n\n'
            f'👥 Invitations validées : <b>{credited}</b>\n'
            f'⏳ En attente : <b>{pending}</b>\n'
            f'📨 Total observé : <b>{total}</b>\n\n'
            f'Ton lien personnel :\n<code>{escape(link)}</code>')


def leaderboard_view(rows, lang='fr'):
    en = lang == 'en'
    lines = ['<b>🏆 TOP 10 INVITATIONS</b>', '']
    if not rows:
        lines.append('No validated invitations yet.' if en else 'Aucune invitation validée pour le moment.')
    for i, row in enumerate(rows[:10], 1):
        lines.append(f'{i:02d}. {escape(mask_display(row["display_name"], row["referrer_id"]))} — <b>{int(row["n"])}</b>')
    lines.extend(['', 'Seules les invitations validées comptent.' if not en else 'Only validated invitations count.'])
    return '\n'.join(lines)
