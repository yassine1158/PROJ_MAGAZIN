"""Impression des bons (entrée, sortie, inventaire) en PDF.

Pour un nouveau type de bon : écrire une fonction `_mon_bon(bon)` qui renvoie la liste des éléments
de la page, puis l'enregistrer avec `GENERATEURS[MonBon] = _mon_bon`. Briques disponibles :
- _entete(bon, titre) : société (logo, adresse, RCCM, NCC), titre « <titre> N° <numéro> », exemplaire et statut ;
- _infos([(libellé, valeur), …]) : bloc d'informations sur deux colonnes (une valeur longue prend toute la largeur) ;
- _tableau(entetes, lignes, total=None, largeurs=None, droite=None) : tableau des articles, en-tête répété
  sur chaque page, code (1re colonne) replié s'il est long ;
- _somme_en_lettres(montant) : ligne « Arrêté le présent bon à la somme de : … » ;
- _signatures(gauche, droite, nom_gauche='', nom_droite='') : deux cases « Nom, date, signature », jamais coupées ;
- _date(bon), _nom(utilisateur), _validation(bon), _unite(article).
Le pied de page (numéro, page x/y, date d'impression, utilisateur) et le filigrane ANNULÉ / BROUILLON
sont ajoutés par reponse_pdf sur chaque page, quel que soit le type de bon.
"""
import re
from collections import Counter
from decimal import Decimal
from xml.sax.saxutils import escape

from django.http import HttpResponse
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    Flowable, HRFlowable, Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from .models import BonEntree, BonSortie, Inventaire, Parametres
from .utils import montant_en_lettres, nombre, quantite

BLEU = colors.HexColor('#1f497d')
GRIS = colors.HexColor('#5f6b7a')
GRIS_CLAIR = colors.HexColor('#eef1f5')
TRAIT = colors.HexColor('#aab3bf')
LARGEUR = 180 * mm  # largeur utile d'une page A4 (marges de 15 mm)
MARGE_CELLULE = 4  # points, à gauche et à droite de chaque case de tableau

STYLES = getSampleStyleSheet()
TITRE = ParagraphStyle('Titre', parent=STYLES['Heading1'], alignment=TA_CENTER, textColor=BLEU, spaceAfter=2)
NORMAL = STYLES['Normal']
PETIT = ParagraphStyle('Petit', parent=NORMAL, fontSize=9, leading=11)
CODE = ParagraphStyle('Code', parent=NORMAL, fontSize=8.5, leading=10)
ENTETE = ParagraphStyle('Entete', parent=NORMAL, fontName='Helvetica-Bold', fontSize=8.5, leading=10,
                        textColor=colors.white)
ENTETE_DROITE = ParagraphStyle('EnteteDroite', parent=ENTETE, alignment=TA_RIGHT)
LIBELLE = ParagraphStyle('Libelle', parent=NORMAL, fontName='Helvetica-Bold', fontSize=8, leading=10, textColor=GRIS)
VALEUR = ParagraphStyle('Valeur', parent=NORMAL, fontSize=9.5, leading=12)


def _nom(utilisateur):
    """Nom affiché d'un utilisateur : « Prénom Nom », sinon l'identifiant."""
    if not utilisateur:
        return ''
    return utilisateur.get_full_name() or utilisateur.get_username()


def _date(bon):
    return timezone.localtime(bon.date).strftime('%d/%m/%Y %H:%M')


def _validation(bon):
    """« Awa Koné le 08/10/2026 à 19:20 » (vide si le bon n'a jamais été validé)."""
    if not bon.valide_par_id:
        return ''
    texte = _nom(bon.valide_par)
    if bon.date_validation:
        quand = timezone.localtime(bon.date_validation)
        texte += f' le {quand:%d/%m/%Y} à {quand:%H:%M}'
    return texte


def _unite(article):
    """Unité en clair : « Sac », « Mètre carré »…"""
    return article.get_unite_display()


def _entete(bon, titre):
    """Haut du bon : logo et coordonnées de la société (Réglages › Ma société), titre « <titre> N° <numéro> »,
    exemplaire (impression en 2 exemplaires) et statut si le bon n'est pas validé."""
    p = Parametres.actuels()
    coordonnees = [f'<font size="12"><b>{escape(p.nom_societe)}</b></font>'] if p.nom_societe else []
    coordonnees += [escape(p.adresse)] if p.adresse else []
    coordonnees += [f'Tél. : {escape(p.telephone)}'] if p.telephone else []
    legal = ' · '.join(f'{k} : {escape(v)}' for k, v in (('RCCM', p.rccm), ('NCC', p.ncc)) if v)
    if legal:
        coordonnees.append(f'<font color="#5f6b7a">{legal}</font>')
    texte = Paragraph('<br/>'.join(coordonnees) or '&nbsp;', ParagraphStyle(
        'Societe', parent=NORMAL, fontSize=9, leading=13, alignment=TA_RIGHT if p.logo else TA_LEFT))
    logo = None
    if p.logo:
        try:
            logo = Image(p.logo.path, width=45 * mm, height=20 * mm, kind='proportional')
            logo.hAlign = 'LEFT'
        except OSError:
            logo = None
    if logo:
        entete = Table([[logo, texte]], colWidths=[70 * mm, 110 * mm])
        entete.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'MIDDLE'), ('LEFTPADDING', (0, 0), (-1, -1), 0),
                                    ('RIGHTPADDING', (0, 0), (-1, -1), 0)]))
    else:
        entete = texte
    elements = [entete, Spacer(1, 3 * mm), HRFlowable(width='100%', thickness=1.2, color=BLEU, spaceAfter=5 * mm),
                Paragraph(f'{titre} N° {escape(bon.numero)}', TITRE)]
    exemplaire = getattr(bon, '_exemplaire', '')
    if exemplaire:
        cartouche = Table([[exemplaire.upper()]], hAlign='CENTER')
        cartouche.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (-1, -1), 'Helvetica-Bold'), ('FONTSIZE', (0, 0), (-1, -1), 8.5),
            ('TEXTCOLOR', (0, 0), (-1, -1), BLEU), ('BOX', (0, 0), (-1, -1), 0.8, BLEU),
            ('LEFTPADDING', (0, 0), (-1, -1), 8), ('RIGHTPADDING', (0, 0), (-1, -1), 8),
            ('TOPPADDING', (0, 0), (-1, -1), 2), ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ]))
        elements += [Spacer(1, 1 * mm), cartouche]
    if bon.statut != bon.VALIDE:
        elements.append(Paragraph(f'<font color="#c9302c"><b>{bon.get_statut_display().upper()}</b></font>',
                                  ParagraphStyle('Statut', parent=NORMAL, alignment=TA_CENTER, spaceBefore=4)))
    elements.append(Spacer(1, 5 * mm))
    return elements


def _infos(lignes):
    """Bloc d'informations : [(libellé, valeur), …] rangés deux par deux ; une valeur longue prend toute la largeur."""
    courtes, longues = [], []
    for libelle, valeur in lignes:
        texte = str(valeur if valeur not in (None, '') else '—')
        (longues if len(texte) > 42 else courtes).append(
            (Paragraph(escape(libelle), LIBELLE), Paragraph(escape(texte).replace('\n', '<br/>'), VALEUR)))
    if len(courtes) % 2:
        longues.insert(0, courtes.pop())
    paires = len(courtes) // 2
    data = [[*courtes[2 * i], *courtes[2 * i + 1]] for i in range(paires)] + [[l, v, '', ''] for l, v in longues]
    if not data:
        return []
    table = Table(data, colWidths=[27 * mm, 63 * mm, 27 * mm, 63 * mm])
    style = [
        ('BOX', (0, 0), (-1, -1), 0.6, TRAIT),
        ('INNERGRID', (0, 0), (-1, -1), 0.3, TRAIT),
        ('BACKGROUND', (0, 0), (0, -1), GRIS_CLAIR),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]
    if paires:
        style.append(('BACKGROUND', (2, 0), (2, paires - 1), GRIS_CLAIR))
    style += [('SPAN', (1, i), (3, i)) for i in range(paires, len(data))]
    table.setStyle(TableStyle(style))
    return [table, Spacer(1, 6 * mm)]


def _replier(texte, largeur, police='Helvetica', taille=CODE.fontSize):
    """Coupe un texte sans espace (code article) en lignes qui tiennent dans la largeur (en points),
    de préférence après un tiret, une barre, un point ou un espace."""
    def mesure(t):
        return stringWidth(t, police, taille)

    lignes, ligne = [], ''
    for morceau in re.findall(r'[^-/._ ]*[-/._ ]?', texte):
        if not morceau:
            continue
        if ligne and mesure(ligne + morceau) > largeur:
            lignes.append(ligne)
            ligne = ''
        ligne += morceau
        while len(ligne) > 1 and mesure(ligne) > largeur:
            # Morceau sans séparateur trop long : coupé en lignes de longueurs égales (pas de « 23 » seul en bas).
            cible = mesure(ligne) / -(-mesure(ligne) // largeur)
            coupe = 1
            while coupe < len(ligne) - 1 and mesure(ligne[:coupe]) < cible:
                coupe += 1
            while coupe > 1 and mesure(ligne[:coupe]) > largeur:
                coupe -= 1
            lignes.append(ligne[:coupe])
            ligne = ligne[coupe:]
    lignes.append(ligne)
    return [l.strip() for l in lignes if l.strip()]


def _ajuster(largeurs, lignes, total):
    """Élargit les colonnes de texte simple (nombres) trop étroites pour leur contenu, en prenant sur la
    colonne la plus large qui contient des paragraphes (ceux-ci se replient) ; celle-ci reçoit aussi la place
    restante pour que le tableau occupe toute la largeur utile."""
    largeurs = list(largeurs)
    souples = [j for j in range(1, len(largeurs)) if any(isinstance(l[j], Flowable) for l in lignes)]
    if souples and sum(largeurs) < LARGEUR:
        largeurs[max(souples, key=lambda k: largeurs[k])] += LARGEUR - sum(largeurs)
    for j in range(1, len(largeurs)):
        if j in souples:
            continue
        besoin = max([stringWidth(str(l[j]), 'Helvetica', 9) for l in lignes] or [0])
        if total is not None and j == len(largeurs) - 1:
            besoin = max(besoin, stringWidth(str(total), 'Helvetica-Bold', 9))
        manque = besoin + 2 * MARGE_CELLULE + 1 - largeurs[j]
        if manque > 0 and souples:
            donneur = max(souples, key=lambda k: largeurs[k])
            pris = min(manque, max(0, largeurs[donneur] - 30 * mm))
            largeurs[donneur] -= pris
            largeurs[j] += pris
    return largeurs


def _tableau(entetes, lignes, total=None, largeurs=None, droite=None):
    """Tableau des lignes du bon, en-tête répété en haut de chaque page.

    entetes : titres des colonnes ; lignes : listes de cases (texte ou Paragraph), la 1re étant le code
    article (replié s'il est trop long) ; total : texte de la ligne TOTAL (dernière colonne) ;
    largeurs : largeurs des colonnes en points (ex. 30 * mm), les colonnes de nombres sont élargies si besoin ;
    droite : numéros des colonnes alignées à droite (par défaut : à partir de la 3e).
    """
    droite = set(range(2, len(entetes)) if droite is None else droite)
    if largeurs:
        largeurs = _ajuster(largeurs, lignes, total)
    data = [[Paragraph(escape(t), ENTETE_DROITE if j in droite else ENTETE) if isinstance(t, str) else t
             for j, t in enumerate(entetes)]]
    for ligne in lignes:
        ligne = list(ligne)
        if isinstance(ligne[0], str):
            morceaux = _replier(ligne[0], largeurs[0] - 2 * MARGE_CELLULE) if largeurs else [ligne[0]]
            ligne[0] = Paragraph('<br/>'.join(escape(m) for m in morceaux), CODE)
        data.append(ligne)
    if total is not None:
        data.append(['TOTAL'] + [''] * (len(entetes) - 2) + [total])
    table = Table(data, colWidths=largeurs, repeatRows=1)
    style = [
        ('BACKGROUND', (0, 0), (-1, 0), BLEU),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('GRID', (0, 0), (-1, -1), 0.4, TRAIT),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), MARGE_CELLULE),
        ('RIGHTPADDING', (0, 0), (-1, -1), MARGE_CELLULE),
        ('ROWBACKGROUNDS', (0, 1), (-1, len(lignes)), [colors.white, colors.HexColor('#f7f9fb')]),
    ]
    style += [('ALIGN', (j, 1), (j, -1), 'RIGHT') for j in sorted(droite)]
    if total is not None:
        style += [
            ('SPAN', (0, -1), (-2, -1)),
            ('ALIGN', (0, -1), (-1, -1), 'RIGHT'),
            ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
            ('BACKGROUND', (0, -1), (-1, -1), GRIS_CLAIR),
        ]
    table.setStyle(TableStyle(style))
    return [table, Spacer(1, 4 * mm)]


def _somme_en_lettres(montant):
    """« Arrêté le présent bon à la somme de : deux millions … francs CFA » (rien si le montant est nul)."""
    montant = Decimal(montant or 0).quantize(Decimal('1'))
    if montant <= 0:
        return []
    lettres = montant_en_lettres(montant)
    devise = Parametres.actuels().devise.strip()
    if devise.upper().replace(' ', '').replace('.', '') in ('FCFA', 'CFA', 'XOF', 'XAF', ''):
        devise = 'franc CFA' if montant == 1 else 'francs CFA'
    if lettres.endswith(('million', 'millions', 'milliard', 'milliards')):
        lettres += ' de'
    return [Paragraph(f'Arrêté le présent bon à la somme de : <b>{escape(lettres)} {escape(devise)}</b>', PETIT),
            Spacer(1, 2 * mm)]


def _signatures(gauche, droite, nom_gauche='', nom_droite=''):
    """Deux cases de signature côte à côte (titre, puis lignes « Nom », « Date », « Signature »), gardées
    ensemble sur la même page. nom_gauche / nom_droite : nom déjà connu, imprimé sur la ligne « Nom »."""
    def case(titre, nom):
        lignes = [
            [Paragraph(f'<b>{escape(titre)}</b>', PETIT), '', ''],
            [Paragraph('Nom :', LIBELLE), Paragraph(escape(nom or ''), PETIT), ''],
            [Paragraph('Date :', LIBELLE), '', ''],
            [Paragraph('Signature :', LIBELLE), '', ''],
        ]
        # 3e colonne vide : les pointillés s'arrêtent avant le bord de la case.
        table = Table(lignes, colWidths=[20 * mm, 61 * mm, 4 * mm], rowHeights=[None, 8 * mm, 8 * mm, 24 * mm])
        table.setStyle(TableStyle([
            ('SPAN', (0, 0), (-1, 0)),
            ('BACKGROUND', (0, 0), (-1, 0), GRIS_CLAIR),
            ('BOX', (0, 0), (-1, -1), 0.6, TRAIT),
            ('LINEBELOW', (0, 0), (-1, 0), 0.6, TRAIT),
            ('LINEBELOW', (1, 1), (1, 2), 0.5, TRAIT, None, (1, 2)),
            ('VALIGN', (0, 1), (-1, 2), 'BOTTOM'),
            ('VALIGN', (0, 3), (-1, 3), 'TOP'),
            ('TOPPADDING', (0, 3), (-1, 3), 6),
            ('BOTTOMPADDING', (0, 1), (-1, 2), 2),
        ]))
        return table

    table = Table([[case(gauche, nom_gauche), '', case(droite, nom_droite)]], colWidths=[85 * mm, 10 * mm, 85 * mm])
    table.setStyle(TableStyle([('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0)]))
    return [KeepTogether([Spacer(1, 6 * mm), table])]


def _colonnes_prix(devise):
    entetes = ['Code', 'Désignation', 'Quantité', 'Unité', 'P.U.', f'Montant ({devise})']
    largeurs = [32 * mm, 58 * mm, 19 * mm, 21 * mm, 23 * mm, 27 * mm]
    return entetes, largeurs, {2, 4, 5}


def _bon_entree(bon):
    p = Parametres.actuels()
    elements = _entete(bon, "BON D'ENTRÉE")
    elements += _infos([
        ('Date', _date(bon)),
        ('Fournisseur', bon.fournisseur),
        ('N° BL / facture', bon.reference),
        ('Établi par', _nom(bon.cree_par)),
        ('Validé par', _validation(bon)),
        ('Remarque', bon.observation),
    ])
    lignes = [
        [l.article.code, Paragraph(escape(l.article.designation), PETIT), quantite(l.quantite), _unite(l.article),
         nombre(l.prix_unitaire), nombre(l.montant)]
        for l in bon.lignes.select_related('article')
    ]
    entetes, largeurs, droite = _colonnes_prix(p.devise)
    total = bon.total()
    elements += _tableau(entetes, lignes, total=nombre(total), largeurs=largeurs, droite=droite)
    elements += _somme_en_lettres(total)
    elements += _signatures('Le livreur', p.signataire, nom_droite=_nom(bon.cree_par))
    return elements


def _bon_sortie(bon):
    p = Parametres.actuels()
    elements = _entete(bon, 'BON DE SORTIE')
    elements += _infos([
        ('Date', _date(bon)),
        ('Chantier', bon.chantier),
        ('Engin', bon.engin),
        ('Reçu par', bon.demandeur),
        ('Établi par', _nom(bon.cree_par)),
        ('Validé par', _validation(bon)),
        ('Remarque', bon.observation),
    ])
    lignes = list(bon.lignes.select_related('article'))
    # Sans prix : réglage « Ne pas imprimer les prix », ou brouillon (prix fixés seulement à la validation).
    if p.masquer_prix_sortie or not any(l.prix_unitaire for l in lignes):
        elements += _tableau(
            ['Code', 'Désignation', 'Quantité', 'Unité'],
            [[l.article.code, Paragraph(escape(l.article.designation), PETIT), quantite(l.quantite),
              _unite(l.article)] for l in lignes],
            largeurs=[34 * mm, 102 * mm, 20 * mm, 24 * mm], droite={2})
    else:
        entetes, largeurs, droite = _colonnes_prix(p.devise)
        total = bon.total()
        elements += _tableau(entetes, [
            [l.article.code, Paragraph(escape(l.article.designation), PETIT), quantite(l.quantite),
             _unite(l.article), nombre(l.prix_unitaire), nombre(l.montant)] for l in lignes
        ], total=nombre(total), largeurs=largeurs, droite=droite)
        elements += _somme_en_lettres(total)
    elements += _signatures(p.signataire, 'Le réceptionnaire', nom_gauche=_nom(bon.cree_par), nom_droite=bon.demandeur)
    return elements


def _inventaire(bon):
    elements = _entete(bon, "FICHE D'INVENTAIRE")
    elements += _infos([('Date', _date(bon)), ('Observation', bon.observation)])
    lignes = [
        [l.article.code, Paragraph(escape(l.article.designation), PETIT),
         quantite(l.stock_theorique if l.stock_theorique is not None else l.article.stock),
         quantite(l.stock_compte), quantite(l.ecart)]
        for l in bon.lignes.select_related('article')
    ]
    elements += _tableau(['Code', 'Désignation', 'Théorique', 'Compté', 'Écart'], lignes,
                         largeurs=[25 * mm, 75 * mm, 25 * mm, 25 * mm, 25 * mm])
    elements += _signatures(Parametres.actuels().signataire, 'Le responsable')
    return elements


GENERATEURS = {BonEntree: _bon_entree, BonSortie: _bon_sortie, Inventaire: _inventaire}

# Destinataire du 2e exemplaire (?exemplaires=2) ; « réceptionnaire » pour les types absents.
DESTINATAIRES = {BonEntree: 'livreur'}


class _Repere(Flowable):
    """Début d'un bon (ou d'un exemplaire) : le pied de page et le filigrane des pages qui suivent s'y rapportent."""

    def __init__(self, groupe):
        super().__init__()
        self.groupe = groupe

    def wrap(self, *args):
        return 0, 0

    def draw(self):
        self.canv._groupe_bon = self.groupe


class _Canevas(Canvas):
    """Garde les pages en mémoire jusqu'à la fin pour écrire « Page 1/2 » (le total n'est connu qu'à la fin),
    puis ajoute le pied de page et le filigrane sur chacune."""
    mention = ''  # « Imprimé le … par … », fixé par reponse_pdf

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pages = []

    def showPage(self):
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        groupes = [page.get('_groupe_bon') or {} for page in self._pages]
        totaux = Counter(g.get('id') for g in groupes)
        vues = Counter()
        for page, groupe in zip(self._pages, groupes):
            self.__dict__.update(page)
            vues[groupe.get('id')] += 1
            if groupe.get('filigrane'):
                self._filigrane(groupe['filigrane'])
            self._pied(groupe.get('numero', ''), vues[groupe.get('id')], totaux[groupe.get('id')])
            super().showPage()
        super().save()

    def _filigrane(self, texte):
        largeur, hauteur = self._pagesize
        taille = min(110, 0.62 * (largeur ** 2 + hauteur ** 2) ** 0.5 / stringWidth(texte, 'Helvetica-Bold', 1))
        self.saveState()
        self.setFillColor(colors.HexColor('#8a94a3'))
        self.setFillAlpha(0.2)
        self.setFont('Helvetica-Bold', taille)
        self.translate(largeur / 2, hauteur / 2)
        self.rotate(54)
        self.drawCentredString(0, -taille / 3, texte)
        self.restoreState()

    def _pied(self, numero, page, total):
        from config import produit

        largeur = self._pagesize[0]
        morceaux = [numero, f'Page {page}/{total}', self.mention, produit.NOM]
        self.saveState()
        self.setStrokeColor(TRAIT)
        self.setLineWidth(0.5)
        self.line(15 * mm, 12 * mm, largeur - 15 * mm, 12 * mm)
        self.setFillColor(GRIS)
        self.setFont('Helvetica', 7.5)
        self.drawCentredString(largeur / 2, 8 * mm, ' · '.join(m for m in morceaux if m))
        self.restoreState()


def reponse_pdf(bons, nom_fichier, utilisateur=None, exemplaires=1):
    """PDF d'un ou plusieurs bons, chacun commençant sur une nouvelle page.

    utilisateur : imprimé en pied de page (« Imprimé le … par … ») ;
    exemplaires=2 : chaque bon est imprimé deux fois, « Exemplaire magasin » puis « Exemplaire réceptionnaire »
    (« Exemplaire livreur » pour un bon d'entrée).
    """
    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = f'inline; filename="{nom_fichier}.pdf"'
    # Marges de 15 mm moins le retrait du cadre de texte (6 points) : 180 mm utiles, comme les tableaux.
    doc = SimpleDocTemplate(response, pagesize=A4, leftMargin=15 * mm - 6, rightMargin=15 * mm - 6,
                            topMargin=12 * mm, bottomMargin=18 * mm, title=nom_fichier,
                            author=Parametres.actuels().nom_societe)
    elements = []
    for bon in bons:
        copies = ['']
        if exemplaires == 2:
            copies = ['Exemplaire magasin', f'Exemplaire {DESTINATAIRES.get(type(bon), "réceptionnaire")}']
        for copie in copies:
            if elements:
                elements.append(PageBreak())
            bon._exemplaire = copie
            filigrane = {bon.ANNULE: 'ANNULÉ', bon.BROUILLON: 'BROUILLON'}.get(bon.statut, '')
            elements.append(_Repere({'id': len(elements), 'numero': bon.numero, 'filigrane': filigrane}))
            elements += GENERATEURS[type(bon)](bon)
        bon._exemplaire = ''
    maintenant = timezone.localtime()
    mention = f'Imprimé le {maintenant:%d/%m/%Y} à {maintenant:%H:%M}'
    if utilisateur:
        mention += f' par {_nom(utilisateur)}'
    doc.build(elements, canvasmaker=type('Canevas', (_Canevas,), {'mention': mention}))
    return response
