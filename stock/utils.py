from decimal import Decimal


def nombre(valeur, decimales=0):
    """1234567.5 -> '1 234 568' (espace comme séparateur de milliers, virgule décimale)."""
    valeur = Decimal(valeur or 0)
    texte = f'{valeur:,.{decimales}f}'.replace(',', ' ').replace('.', ',')
    return texte


def quantite(valeur):
    """Quantité sans zéros inutiles : 12.500 -> '12,5', 3.000 -> '3'."""
    valeur = Decimal(valeur or 0).normalize()
    decimales = max(0, -valeur.as_tuple().exponent)
    return nombre(valeur, decimales)


def nuances(couleur):
    """Couleur principale « #rrggbb » → (couleur, version foncée, version très claire) pour l'interface."""
    try:
        r, g, b = (int(couleur[i:i + 2], 16) for i in (1, 3, 5))
    except (TypeError, ValueError):
        r, g, b = 0x24, 0x57, 0xd6
    fonce = '#%02x%02x%02x' % tuple(int(c * 0.78) for c in (r, g, b))
    clair = '#%02x%02x%02x' % tuple(int(c + (255 - c) * 0.88) for c in (r, g, b))
    return '#%02x%02x%02x' % (r, g, b), fonce, clair
