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
