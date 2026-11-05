"""Étiquettes à coller, imprimées en PDF sur des planches A4.

- planche_articles() : une étiquette par article (ou plusieurs exemplaires) : QR code, code en gros, désignation et
  emplacement ; 24 par page (70 × 37 mm) ou 65 par page (38 × 21 mm), aux dimensions des planches adhésives du
  commerce ;
- etiquettes_etageres() : une grande étiquette par étagère, 4 par page : bloc, code de l'étagère, QR code, niveaux.

Le QR code contient l'adresse d'une route courte du logiciel : /a/<code>/ montre l'article dans « Trouver un
article », /e/<n°>/ la liste des articles de l'étagère. L'appareil photo d'un téléphone qui a accès au logiciel
ouvre directement la page ; une douchette qui lit le QR code tape l'adresse, que la recherche des bons comprend.
"""
import itertools
import re
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlsplit

from django.http import HttpResponse
from django.urls import reverse
from reportlab.graphics.barcode import qrencoder
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas

from .models import Parametres
from .pdf import _replier

NORMAL, GRAS = 'Helvetica', 'Helvetica-Bold'
NOIR = colors.HexColor('#16202a')
GRIS = colors.HexColor('#5f6b7a')
TRAIT = colors.HexColor('#c3cad3')
BLEU = colors.HexColor('#1f497d')


# =========================================================
# Adresses mises dans les QR codes
# =========================================================

def adresse_base(request):
    """« http://192.168.1.20:8765 » : l'adresse que les téléphones utilisent quand leur accès est réglé (module
    reseau), sinon celle par laquelle cette page a été ouverte."""
    try:
        from .reseau import adresse_publique
    except ImportError:
        adresse_publique = None
    base = adresse_publique(request) if adresse_publique else ''
    return (base or request.build_absolute_uri('/')).rstrip('/')


def adresse_locale(base):
    """Vrai si l'adresse ne s'ouvre que sur cet ordinateur (127.0.0.1, localhost) : inutile pour un téléphone."""
    hote = urlsplit(base).hostname or ''
    return hote in ('localhost', '::1') or hote.startswith('127.')


def lien_article(base, article):
    return base + reverse('stock:qr_article', args=[article.code])


def lien_etagere(base, etagere):
    return base + reverse('stock:qr_etagere', args=[etagere.pk])


# =========================================================
# QR code
# =========================================================

@lru_cache(maxsize=256)
def _modules(texte):
    """Cases du QR code (niveau de correction M : reste lisible un peu abîmé ou sali), ligne par ligne."""
    qr = qrencoder.QRCode(None, qrencoder.QRErrorCorrectLevel.M)
    qr.addData(texte)
    try:
        # Masque fixe : tous les lecteurs le lisent, et c'est 4 fois plus rapide que d'essayer les 8 masques
        # (une planche de 1 000 étiquettes en quelques secondes).
        qr.version = qr.calculate_version()
        qr.makeImpl(False, 0)
    except AttributeError:  # autre version de reportlab
        qr.make()
    return tuple(tuple(bool(m) for m in ligne) for ligne in qr.modules)


def _plages(ligne):
    """[(colonne, longueur), …] des cases noires qui se suivent sur une ligne."""
    plages, colonne = [], 0
    for noir, groupe in itertools.groupby(ligne):
        longueur = len(list(groupe))
        if noir:
            plages.append((colonne, longueur))
        colonne += longueur
    return plages


def _dessiner_qr(c, texte, x, y, taille):
    """QR code carré de `taille` points, coin bas gauche en (x, y), sans marge blanche (à prévoir autour)."""
    modules = _modules(texte)
    n = len(modules)
    c.saveState()
    c.translate(x, y)
    c.scale(taille / n, taille / n)
    c.setFillColor(colors.black)
    chemin = c.beginPath()
    for rang, ligne in enumerate(modules):
        for colonne, longueur in _plages(ligne):
            chemin.rect(colonne, n - rang - 1, longueur, 1)
    c.drawPath(chemin, stroke=0, fill=1)
    c.restoreState()


def qr_svg(texte):
    """Le même QR code en SVG (aperçu à l'écran, que l'on peut scanner pour essayer), avec sa marge blanche."""
    modules = _modules(texte)
    n, bord = len(modules), 4
    traits = ''.join(f'M{colonne + bord} {rang + bord}h{longueur}v1h-{longueur}z'
                     for rang, ligne in enumerate(modules) for colonne, longueur in _plages(ligne))
    cote = n + 2 * bord
    return (f'<svg class="qr" viewBox="0 0 {cote} {cote}" shape-rendering="crispEdges" role="img" '
            f'aria-label="QR code"><rect width="{cote}" height="{cote}" fill="#fff"/>'
            f'<path d="{traits}" fill="#000"/></svg>')


# =========================================================
# Textes ajustés à la place disponible
# =========================================================

def _couper_code(code, largeur, grand, petit):
    """(lignes, taille) : le code sur une ligne, aussi grand que possible ; trop long pour tenir en `petit`, il passe
    sur deux lignes (coupées après un tiret de préférence), voire trois pour un code de 50 caractères."""
    taille = min(grand, largeur / max(stringWidth(code, GRAS, 1), 1))
    if taille >= petit:
        return [code], taille
    for lignes_max in (2, 3):
        taille = grand
        while taille >= petit * 0.7:
            lignes = _replier(code, largeur, GRAS, taille)
            if len(lignes) <= lignes_max:
                return lignes, taille
            taille -= 0.5
    return _replier(code, largeur, GRAS, taille)[:3], taille


def _couper_texte(texte, police, taille, largeur, lignes_max):
    """Lignes d'un texte qui tiennent dans la largeur, coupées entre les mots ; s'il en faut plus que
    `lignes_max`, la dernière se termine par « … »."""
    if lignes_max <= 0:
        return []
    texte = re.sub(r'(\d) (?=[^\W\d_])', '\\1\u00a0', ' '.join(texte.split()))  # « 18 kg » jamais coupé
    lignes = []
    for ligne in simpleSplit(texte, police, taille, largeur):
        lignes += _replier(ligne, largeur, police, taille) if stringWidth(ligne, police, taille) > largeur else [ligne]
    if len(lignes) <= lignes_max:
        return lignes
    lignes = lignes[:lignes_max]
    derniere = lignes[-1]
    while derniere and stringWidth(derniere + '…', police, taille) > largeur:
        derniere = derniere.rsplit(' ', 1)[0] if ' ' in derniere.strip() else derniere[:-1]
    lignes[-1] = derniere.rstrip(' ,;:-–(') + '…'
    return lignes


def _emplacement(article, largeur, taille):
    """(texte, taille) : « Bloc A › Étagère R1 › Niv. 2 » ou plus court (« A › R1 › Niv. 2 », « A-R1-N2 ») selon
    la place ; ('', 0) pour un article non rangé."""
    if not article.etagere_id:
        return '', 0
    bloc, etagere, niveau, case = article.etagere.bloc.code, article.etagere.code, article.niveau, article.case
    niv = f' › Niv. {niveau}' if niveau else ''
    formes = [f'Bloc {bloc} › Étagère {etagere}{niv}', f'Bloc {bloc} › Ét. {etagere}{niv}', f'{bloc} › {etagere}{niv}']
    if case:
        formes = [f'{f} › {case}' for f in formes] + formes
    formes.append(f'{bloc}-{etagere}' + (f'-N{niveau}' if niveau else ''))
    for texte in formes:
        if stringWidth(texte, GRAS, taille) <= largeur:
            return texte, taille
    texte = formes[-1]
    return texte, max(3.5, largeur / stringWidth(texte, GRAS, 1))


# =========================================================
# Étiquettes d'articles
# =========================================================

@dataclass(frozen=True)
class Format:
    libelle: str
    detail: str
    colonnes: int
    rangs: int
    largeur: float  # une étiquette
    hauteur: float
    gauche: float  # marges de la planche
    haut: float
    pas_x: float  # d'une étiquette à la suivante (étiquette + espace)
    pas_y: float
    marge_x: float  # blanc à l'intérieur de l'étiquette (les imprimantes n'impriment pas au ras du papier)
    marge_y: float
    qr: float  # côté du QR code
    ecart: float  # entre le QR code et le texte
    code: tuple  # taille du code : la plus grande, la plus petite sur une seule ligne
    texte: float  # désignation
    lieu: float  # emplacement

    @property
    def par_page(self):
        return self.colonnes * self.rangs


FORMATS = {
    'a4-24': Format('24 par page', '70 × 37 mm · grandes étiquettes, lisibles de loin',
                    3, 8, 70 * mm, 37 * mm, 0, 0.5 * mm, 70 * mm, 37 * mm,
                    marge_x=5 * mm, marge_y=4.5 * mm, qr=26 * mm, ecart=3 * mm, code=(15, 9), texte=8, lieu=7.5),
    'a4-65': Format('65 par page', '38 × 21 mm · petites pièces, tiroirs, bacs',
                    5, 13, 38.1 * mm, 21.2 * mm, 4.7 * mm, 10.7 * mm, 40.6 * mm, 21.2 * mm,
                    marge_x=1.6 * mm, marge_y=1.6 * mm, qr=16.5 * mm, ecart=1.4 * mm, code=(9, 6), texte=5.5,
                    lieu=5.5),
}
FORMAT_DEFAUT = 'a4-24'


def _largeur_texte(f):
    return f.largeur - 2 * f.marge_x - f.qr - f.ecart


def texte_emplacement(article, format_=FORMAT_DEFAUT):
    """L'emplacement tel qu'il est imprimé sur l'étiquette de ce format (aperçu à l'écran)."""
    f = FORMATS[format_]
    return _emplacement(article, _largeur_texte(f), f.lieu)[0]


def nombre_pages(nb_etiquettes, format_=FORMAT_DEFAUT, depart=1):
    """Pages nécessaires quand on commence à l'étiquette n° `depart` de la première planche."""
    if nb_etiquettes <= 0:
        return 0
    return -(-(nb_etiquettes + depart - 1) // FORMATS[format_].par_page)


def _document(nom_fichier, titre):
    reponse = HttpResponse(content_type='application/pdf')
    reponse['Content-Disposition'] = f'inline; filename="{nom_fichier}.pdf"'
    c = Canvas(reponse, pagesize=A4)
    c.setTitle(titre)
    c.setAuthor(Parametres.actuels().nom_societe)
    return reponse, c


def _etiquette_article(c, f, x, y, article, lien, contours):
    if contours:
        c.setStrokeColor(TRAIT)
        c.setLineWidth(0.4)
        c.roundRect(x, y, f.largeur, f.hauteur, 1.5 * mm, stroke=1, fill=0)
    _dessiner_qr(c, lien, x + f.marge_x, y + (f.hauteur - f.qr) / 2, f.qr)
    gauche, largeur = x + f.marge_x + f.qr + f.ecart, _largeur_texte(f)
    haut, bas = y + f.hauteur - f.marge_y, y + f.marge_y

    c.setFillColor(NOIR)
    lignes, taille = _couper_code(article.code, largeur, *f.code)
    c.setFont(GRAS, taille)
    ligne_y = haut - 0.74 * taille
    for ligne in lignes:
        c.drawString(gauche, ligne_y, ligne)
        ligne_y -= 1.08 * taille
    haut_texte = ligne_y + 1.08 * taille - 0.24 * taille - 0.25 * f.texte

    lieu, taille_lieu = _emplacement(article, largeur, f.lieu)
    if lieu:
        c.setFont(GRAS, taille_lieu)
        c.drawString(gauche, bas + 0.2 * taille_lieu, lieu)
        bas += 1.35 * taille_lieu

    interligne = 1.18 * f.texte
    place = haut_texte - bas
    lignes_max = int((place - 0.2 * f.texte) // interligne)
    designation = _couper_texte(article.designation, NORMAL, f.texte, largeur, lignes_max)
    c.setFont(NORMAL, f.texte)
    c.setFillColor(colors.HexColor('#2b3540'))
    ligne_y = haut_texte - 0.95 * f.texte
    for ligne in designation:
        c.drawString(gauche, ligne_y, ligne)
        ligne_y -= interligne


def planche_articles(articles, base, format_=FORMAT_DEFAUT, exemplaires=1, depart=1, contours=False,
                     nom_fichier='etiquettes'):
    """PDF des étiquettes d'articles, dans l'ordre donné (chaque article répété `exemplaires` fois), en commençant
    à l'étiquette n° `depart` de la première planche (planche déjà entamée)."""
    f = FORMATS[format_]
    reponse, c = _document(nom_fichier, 'Étiquettes des articles')
    position = max(1, min(depart, f.par_page)) - 1
    for article in articles:
        lien = lien_article(base, article)
        for _ in range(exemplaires):
            if position == f.par_page:
                c.showPage()
                position = 0
            colonne, rang = position % f.colonnes, position // f.colonnes
            x = f.gauche + colonne * f.pas_x
            y = A4[1] - f.haut - f.hauteur - rang * f.pas_y
            _etiquette_article(c, f, x, y, article, lien, contours)
            position += 1
    c.showPage()
    c.save()
    return reponse


# =========================================================
# Étiquettes d'étagères
# =========================================================

ETAGERES_PAR_PAGE = 4


def couleurs_bandeau(couleur):
    """(fond, texte) du bandeau d'un bloc, en « #rrggbb » : sa couleur du plan, texte blanc sur une couleur
    foncée et presque noir sur une couleur claire."""
    try:
        fond = colors.HexColor(couleur)
    except ValueError:
        fond = BLEU
    clair = 0.299 * fond.red + 0.587 * fond.green + 0.114 * fond.blue >= 0.62
    return '#' + fond.hexval()[2:], '#16202a' if clair else '#ffffff'


def _etiquette_etagere(c, x, y, largeur, hauteur, etagere, lien):
    bloc = etagere.bloc
    m = 8 * mm
    utile = largeur - 2 * m
    fond, texte = couleurs_bandeau(bloc.couleur)

    # Bandeau du bloc, à la couleur de la vue 3D.
    bandeau = 22 * mm
    haut = y + hauteur - m
    c.setFillColor(colors.HexColor(fond))
    c.roundRect(x + m, haut - bandeau, utile, bandeau, 2.5 * mm, stroke=0, fill=1)
    c.setFillColor(colors.HexColor(texte))
    titre = f'BLOC {bloc.code}'
    taille = min(24, (utile - 10 * mm) / stringWidth(titre, GRAS, 1))
    if bloc.nom:
        c.setFont(GRAS, taille)
        c.drawString(x + m + 5 * mm, haut - 10.5 * mm, titre)
        nom = _couper_texte(bloc.nom, NORMAL, 10.5, utile - 10 * mm, 1)
        c.setFont(NORMAL, 10.5)
        c.drawString(x + m + 5 * mm, haut - 17.5 * mm, nom[0] if nom else '')
    else:
        c.setFont(GRAS, taille)
        c.drawString(x + m + 5 * mm, haut - bandeau / 2 - 0.36 * taille, titre)

    # Code de l'étagère en très gros.
    c.setFillColor(NOIR)
    taille = min(96, utile / stringWidth(etagere.code, GRAS, 1))
    ligne_y = haut - bandeau - 6 * mm - 0.72 * taille
    c.setFont(GRAS, taille)
    c.drawCentredString(x + largeur / 2, ligne_y, etagere.code)
    sous_titre = f'Bloc {bloc.code} – Étagère {etagere.code}'
    taille_sous = min(13, utile / stringWidth(sous_titre, NORMAL, 1))
    c.setFont(NORMAL, taille_sous)
    c.setFillColor(GRIS)
    c.drawCentredString(x + largeur / 2, ligne_y - 0.22 * taille - 5 * mm, sous_titre)  # sous la queue d'un « Q »

    # QR code à gauche, niveaux à droite (1 = en bas, comme sur l'étagère).
    cote = 50 * mm
    bas = y + m + 9 * mm
    _dessiner_qr(c, lien, x + m, bas, cote)
    gauche = x + m + cote + 6 * mm
    large = x + largeur - m - gauche
    niveaux = max(etagere.nb_niveaux, 0)
    pas = min(9 * mm, (cote - 6 * mm) / niveaux) if niveaux else 0
    c.setFillColor(GRIS)
    c.setFont(GRAS, 8.5)
    c.drawString(gauche, bas + niveaux * pas + 1.5 * mm, 'NIVEAUX')
    if niveaux:
        taille = max(5, min(10, pas * 0.5))
        for n in range(1, niveaux + 1):
            case_y = bas + (n - 1) * pas
            c.setStrokeColor(TRAIT)
            c.setLineWidth(0.8)
            c.setFillColor(colors.HexColor('#f3f5f7'))
            c.rect(gauche, case_y, large, pas - 0.8 * mm, stroke=1, fill=1)
            c.setFillColor(NOIR)
            c.setFont(GRAS if n == 1 else NORMAL, taille)
            texte = f'Niveau {n}' + (' (bas)' if n == 1 else '')
            if stringWidth(texte, NORMAL, taille) > large - 2 * mm:
                texte = f'N{n}'
            c.drawCentredString(gauche + large / 2, case_y + (pas - 0.8 * mm) / 2 - 0.35 * taille, texte)

    c.setFillColor(GRIS)
    c.setFont(NORMAL, 8.5)
    c.drawCentredString(x + largeur / 2, y + m + 1 * mm, 'Scannez le QR code : liste des articles de cette étagère')


def etiquettes_etageres(etageres, base, nom_fichier='etiquettes_etageres'):
    """PDF d'une grande étiquette par étagère (A6, 4 par page A4, traits de coupe en pointillés)."""
    reponse, c = _document(nom_fichier, 'Étiquettes des étagères')
    largeur, hauteur = A4[0] / 2, A4[1] / 2
    for i, etagere in enumerate(etageres):
        position = i % ETAGERES_PAR_PAGE
        if i and not position:
            c.showPage()
        if not position:
            c.saveState()
            c.setStrokeColor(TRAIT)
            c.setLineWidth(0.5)
            c.setDash(3, 3)
            c.line(largeur, 0, largeur, A4[1])
            c.line(0, hauteur, A4[0], hauteur)
            c.restoreState()
        x, y = (position % 2) * largeur, A4[1] - (position // 2 + 1) * hauteur
        _etiquette_etagere(c, x, y, largeur, hauteur, etagere, lien_etagere(base, etagere))
    c.showPage()
    c.save()
    return reponse


# =========================================================
# Affiche « Demande de pièces » (A4, à coller à l'atelier)
# =========================================================

def lien_demande(base):
    return base + reverse('stock:demande')


def affiche_demande(base, nom_fichier='affiche_demande_pieces'):
    """Une page A4 : grand QR code du formulaire de demande de pièces, et le mode d'emploi en trois lignes."""
    reponse, c = _document(nom_fichier, 'Demande de pièces')
    largeur, hauteur = A4
    p = Parametres.actuels()
    fond, texte = couleurs_bandeau(p.couleur or '#1f497d')
    c.setFillColor(colors.HexColor(fond))
    c.rect(0, hauteur - 48 * mm, largeur, 48 * mm, stroke=0, fill=1)
    c.setFillColor(colors.HexColor(texte))
    c.setFont(GRAS, 36)
    c.drawCentredString(largeur / 2, hauteur - 26 * mm, 'DEMANDE DE PIÈCES')
    c.setFont(NORMAL, 15)
    c.drawCentredString(largeur / 2, hauteur - 38 * mm, p.nom_societe or 'Magasin')

    lien = lien_demande(base)
    cote = 120 * mm
    bas = hauteur - 60 * mm - cote
    _dessiner_qr(c, lien, (largeur - cote) / 2, bas, cote)

    c.setFillColor(NOIR)
    y = bas - 16 * mm
    for numero, etape in enumerate(['Connectez le téléphone au Wi-Fi du magasin.',
                                    'Scannez ce QR code avec l\'appareil photo.',
                                    'Remplissez la demande : le magasin la reçoit tout de suite.'], start=1):
        c.setFont(GRAS, 16)
        c.drawString(30 * mm, y, f'{numero}.')
        c.setFont(NORMAL, 16)
        c.drawString(40 * mm, y, etape)
        y -= 11 * mm
    c.setFillColor(GRIS)
    c.setFont(NORMAL, 10)
    c.drawCentredString(largeur / 2, 18 * mm, f'Adresse : {lien}')
    c.drawCentredString(largeur / 2, 12 * mm, 'Si le QR code ne s\'ouvre plus, réimprimez cette affiche '
                        '(Demandes de pièces › Affiche QR code).')
    c.showPage()
    c.save()
    return reponse
