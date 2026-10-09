from decimal import Decimal


def nombre(valeur, decimales=0):
    """1234567.5 -> '1 234 568' (virgule décimale ; espace insécable entre les milliers, pour que
    « 2 080 000 » ne soit jamais coupé en fin de ligne)."""
    valeur = Decimal(valeur or 0)
    texte = f'{valeur:,.{decimales}f}'.replace(',', '\xa0').replace('.', ',')
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


_UNITES = ['zéro', 'un', 'deux', 'trois', 'quatre', 'cinq', 'six', 'sept', 'huit', 'neuf', 'dix', 'onze', 'douze',
           'treize', 'quatorze', 'quinze', 'seize']
_DIZAINES = {2: 'vingt', 3: 'trente', 4: 'quarante', 5: 'cinquante', 6: 'soixante'}


def _moins_de_cent(n):
    if n < 17:
        return _UNITES[n]
    dizaine, unite = divmod(n, 10)
    if dizaine == 1:
        return 'dix-' + _UNITES[unite]
    if dizaine == 7:
        return 'soixante et onze' if unite == 1 else 'soixante-' + _moins_de_cent(10 + unite)
    if dizaine in (8, 9):
        if n == 80:
            return 'quatre-vingts'
        return 'quatre-vingt-' + _moins_de_cent(n - 80)
    if unite == 0:
        return _DIZAINES[dizaine]
    return _DIZAINES[dizaine] + (' et un' if unite == 1 else '-' + _UNITES[unite])


def _moins_de_mille(n):
    centaine, reste = divmod(n, 100)
    if not centaine:
        return _moins_de_cent(reste)
    tete = 'cent' if centaine == 1 else _UNITES[centaine] + ' cent'
    if not reste:
        return tete + ('s' if centaine > 1 else '')
    return f'{tete} {_moins_de_cent(reste)}'


def montant_en_lettres(valeur):
    """Montant en toutes lettres (arrondi à l'unité), orthographe traditionnelle.

    2080500 -> 'deux millions quatre-vingt mille cinq cents' ; 81 -> 'quatre-vingt-un' ; 21 -> 'vingt et un'.
    """
    n = int(Decimal(valeur or 0).quantize(Decimal('1')))
    if n == 0:
        return 'zéro'
    if n < 0:
        return 'moins ' + montant_en_lettres(-n)
    morceaux = []
    for taille, nom in ((10 ** 9, 'milliard'), (10 ** 6, 'million')):
        nb, n = divmod(n, taille)
        if nb:
            morceaux.append(f'{montant_en_lettres(nb)} {nom}{"s" if nb > 1 else ""}')
    milliers, n = divmod(n, 1000)
    if milliers:
        # « mille » est invariable, et vingt / cent perdent leur « s » devant lui.
        texte = _moins_de_mille(milliers)
        if texte.endswith(('cents', 'vingts')):
            texte = texte[:-1]
        morceaux.append('mille' if milliers == 1 else f'{texte} mille')
    if n:
        morceaux.append(_moins_de_mille(n))
    return ' '.join(morceaux)
