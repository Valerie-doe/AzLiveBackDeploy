import random

PLACEHOLDER_NAMES = {'Client Live', 'Client Facebook', 'Client TikTok'}


def first_name(nom: str | None) -> str:
    if not nom:
        return ''
    cleaned = nom.strip()
    if cleaned in PLACEHOLDER_NAMES:
        return ''
    return cleaned.split()[0]


def pick(options: list[str]) -> str:
    """Tire une variante au hasard (rotation des tournures)."""
    return random.choice(options)


def greeting(nom: str | None = None) -> str:
    prenom = first_name(nom)
    if prenom:
        base = pick(['Salama', 'Manao ahoana', 'Miarahaba anao', 'Salama e'])
        return f'{base} {prenom}'
    return pick(['Salama tompoko', 'Manao ahoana tompoko', 'Miarahaba anao'])


def thanks() -> str:
    return pick(['Misaotra', 'Misaotra betsaka', 'Misaotra indrindra', 'Misaotra tompoko'])


def thanks_with_name(nom: str | None = None) -> str:
    """Remerciement naturel, sans doubler « tompoko » / « betsaka » devant un prénom."""
    prenom = first_name(nom)
    if prenom and len(prenom) > 2:
        return pick([
            f'Misaotra betsaka {prenom}',
            f'Misaotra indrindra {prenom}',
            f'Misaotra {prenom}',
        ])
    return pick(['Misaotra betsaka', 'Misaotra indrindra', 'Misaotra tompoko'])


def emoji(prob: float = 0.5, choices: list[str] | None = None) -> str:
    choices = choices or ['😊', '🙏', '❤️', '🥰']
    if random.random() < prob:
        return ' ' + random.choice(choices)
    return ''
