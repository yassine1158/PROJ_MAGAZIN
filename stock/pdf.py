"""Impression des bons (entrée, sortie, inventaire) en PDF.

Pour un nouveau type de bon : écrire une fonction `_mon_bon(bon)` qui renvoie la liste des éléments
de la page, puis l'enregistrer avec `GENERATEURS[MonBon] = _mon_bon`. Briques disponibles :
- _societe() : logo et coordonnées de la société (adresse, RCCM, NCC), suivis d'un trait ;
- _entete(bon, titre) : société, titre « <titre> N° <numéro> », exemplaire et statut ;
- _infos([(libellé, valeur), …]) : bloc d'informations sur deux colonnes (une valeur longue prend toute la largeur) ;
- _tableau(entetes, lignes, total=None, largeurs=None, droite=None) : tableau des articles, en-tête répété
  sur chaque page, code (1re colonne) replié s'il est long ;
- _somme_en_lettres(montant) : ligne « Arrêté le présent bon à la somme de : … » ;
- _signatures(gauche, droite, nom_gauche='', nom_droite='') : deux cases « Nom, date, signature », jamais coupées ;
- _date(bon), _nom(utilisateur), _validation(bon), _unite(article).
Le pied de page (numéro, page x/y, date d'impression, utilisateur) et le filigrane ANNULÉ / BROUILLON
sont ajoutés par reponse_pdf sur chaque page, quel que soit le type de bon.

Rapports (consommation, état du stock à une date) : rapport_consommation() et rapport_etat_stock(), avec le
même en-tête société et le même pied de page.
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


def _societe():
    """Logo et coordonnées de la société (Réglages › Ma société), en haut de chaque document."""
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
    return [entete, Spacer(1, 3 * mm), HRFlowable(width='100%', thickness=1.2, color=BLEU, spaceAfter=5 * mm)]


def _entete(bon, titre):
    """Haut du bon : société, titre « <titre> N° <numéro> », exemplaire (impression en 2 exemplaires)
    et statut si le bon n'est pas validé."""
    elements = _societe() + [Paragraph(f'{titre} N° {escape(bon.numero)}', TITRE)]
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


def _tableau(entetes, lignes, total=None, largeurs=None, droite=None, libelle_total='TOTAL'):
    """Tableau des lignes du bon, en-tête répété en haut de chaque page.

    entetes : titres des colonnes ; lignes : listes de cases (texte ou Paragraph), la 1re étant le code
    article (replié s'il est trop long) ; total : texte de la ligne TOTAL (dernière colonne) ;
    largeurs : largeurs des colonnes en points (ex. 30 * mm), les colonnes de nombres sont élargies si besoin ;
    droite : numéros des colonnes alignées à droite (par défaut : à partir de la 3e) ;
    libelle_total : texte de la case de gauche de la ligne de total (« Sous-total » dans les rapports).
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
        data.append([libelle_total] + [''] * (len(entetes) - 2) + [total])
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


def _fin_signee(fin, gauche, droite):
    """La fin d'un rapport (total, notes…) et les signatures, gardées ensemble sur une même page. Les signatures sont
    ajoutées sans leur propre KeepTogether : imbriqué, il ferait toujours passer le tout à la page suivante."""
    return KeepTogether(fin + _signatures(gauche, droite)[0]._content)


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
         'non compté' if l.stock_compte is None else quantite(l.stock_compte), '—' if l.ecart is None else quantite(l.ecart)]
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


def _document(nom_fichier):
    """(réponse HTTP, document A4) ; marges de 15 mm moins le retrait du cadre de texte (6 points) :
    180 mm utiles, comme les tableaux."""
    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = f'inline; filename="{nom_fichier}.pdf"'
    doc = SimpleDocTemplate(response, pagesize=A4, leftMargin=15 * mm - 6, rightMargin=15 * mm - 6,
                            topMargin=12 * mm, bottomMargin=18 * mm, title=nom_fichier,
                            author=Parametres.actuels().nom_societe)
    return response, doc


def _construire(doc, elements, utilisateur):
    """Mise en pages, avec pied de page « Imprimé le … par … » et numéros de page."""
    maintenant = timezone.localtime()
    mention = f'Imprimé le {maintenant:%d/%m/%Y} à {maintenant:%H:%M}'
    if utilisateur:
        mention += f' par {_nom(utilisateur)}'
    doc.build(elements, canvasmaker=type('Canevas', (_Canevas,), {'mention': mention}))


def reponse_pdf(bons, nom_fichier, utilisateur=None, exemplaires=1):
    """PDF d'un ou plusieurs bons, chacun commençant sur une nouvelle page.

    utilisateur : imprimé en pied de page (« Imprimé le … par … ») ;
    exemplaires=2 : chaque bon est imprimé deux fois, « Exemplaire magasin » puis « Exemplaire réceptionnaire »
    (« Exemplaire livreur » pour un bon d'entrée).
    """
    response, doc = _document(nom_fichier)
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
    _construire(doc, elements, utilisateur)
    return response


# =========================================================
# Rapports : consommation, état du stock à une date
# =========================================================

SOUS_TITRE = ParagraphStyle('SousTitre', parent=NORMAL, alignment=TA_CENTER, textColor=GRIS, fontSize=9.5,
                            leading=13)
GROUPE = ParagraphStyle('Groupe', parent=NORMAL, fontName='Helvetica-Bold', fontSize=10.5, leading=13,
                        textColor=BLEU, spaceBefore=6, spaceAfter=4, keepWithNext=1)
NOTE = ParagraphStyle('Note', parent=PETIT, textColor=GRIS, fontSize=8.5, leading=11)


def _entete_rapport(titre, sous_titre=''):
    elements = _societe() + [Paragraph(escape(titre), TITRE)]
    if sous_titre:
        elements.append(Paragraph(escape(sous_titre), SOUS_TITRE))
    return elements + [Spacer(1, 5 * mm)]


def _recapitulatif(titre_colonne, groupes, total, devise, valeur='Valeur'):
    """Tableau d'un coup d'œil : un total par groupe (chantier, engin, catégorie…) et le total général."""
    lignes = [[Paragraph(escape(g['nom']), PETIT), str(len(g['lignes'])), nombre(g['total'])] for g in groupes]
    return ([Paragraph('Récapitulatif', GROUPE)]
            + _tableau([titre_colonne, 'Articles', f'{valeur} ({devise})'], lignes, total=nombre(total),
                       largeurs=[124 * mm, 22 * mm, 34 * mm], droite={1, 2}))


def _total_general(montant, devise):
    table = Table([['TOTAL GÉNÉRAL', f'{nombre(montant)} {devise}']], colWidths=[130 * mm, 50 * mm])
    table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica-Bold'), ('FONTSIZE', (0, 0), (-1, -1), 10.5),
        ('BACKGROUND', (0, 0), (-1, -1), BLEU), ('TEXTCOLOR', (0, 0), (-1, -1), colors.white),
        ('ALIGN', (0, 0), (-1, -1), 'RIGHT'), ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6), ('RIGHTPADDING', (0, 0), (-1, -1), MARGE_CELLULE),
    ]))
    return [Spacer(1, 2 * mm), table, Spacer(1, 4 * mm)]


def _rapport(elements, nom_fichier, repere, utilisateur):
    """PDF d'un rapport ; `repere` est rappelé dans le pied de chaque page (« État du stock au … · Page 1/2 »)."""
    response, doc = _document(nom_fichier)
    _construire(doc, [_Repere({'id': 0, 'numero': repere, 'filigrane': ''})] + elements, utilisateur)
    return response


def rapport_consommation(donnees, du, au, par, chantier=None, utilisateur=None):
    """donnees : résultat de rapports.consommation()."""
    devise = Parametres.actuels().devise
    titres = {'chantier': ('CONSOMMATION PAR CHANTIER', 'Chantier'), 'engin': ('CONSOMMATION PAR ENGIN', 'Engin'),
              'article': ('CONSOMMATION PAR ARTICLE', 'Article')}
    titre, colonne = titres.get(par, titres['chantier'])
    periode = f'Du {du:%d/%m/%Y} au {au:%d/%m/%Y}'
    sous_titre = ' · '.join(filter(None, [periode, f'Chantier : {chantier.nom}' if chantier else '',
                                          'sorties validées moins les retours, au prix moyen']))
    elements = _entete_rapport(titre, sous_titre)
    entetes = ['Code', 'Désignation', 'Quantité', 'Unité', f'Valeur ({devise})']
    largeurs = [32 * mm, 84 * mm, 20 * mm, 20 * mm, 24 * mm]

    def lignes(groupe):
        return [[l['code'], Paragraph(escape(l['designation']), PETIT), quantite(l['qte']), l['unite'],
                 nombre(l['valeur'])] for l in groupe['lignes']]

    groupes = donnees['groupes']
    if not groupes:
        elements.append(Paragraph('Aucune sortie sur cette période.', NORMAL))
    elif par == 'article':
        elements += _tableau(entetes, lignes(groupes[0]), total=nombre(donnees['total']), largeurs=largeurs,
                             droite={2, 4})
    else:
        elements += _recapitulatif(colonne, groupes, donnees['total'], devise)
        for groupe in groupes:
            elements.append(Paragraph(escape(groupe['nom']), GROUPE))
            elements += _tableau(entetes, lignes(groupe), total=nombre(groupe['total']), largeurs=largeurs,
                                 droite={2, 4}, libelle_total='Sous-total')
        elements += _total_general(donnees['total'], devise)
    return _rapport(elements, f'consommation_{du:%Y-%m-%d}_au_{au:%Y-%m-%d}', f'Consommation · {periode}',
                    utilisateur)


def rapport_etat_stock(donnees, jour, par, utilisateur=None):
    """donnees : résultat de rapports.etat_a_date() ; cases de signature en bas."""
    p = Parametres.actuels()
    regroupement = 'par bloc' if par == 'bloc' else 'par catégorie'
    elements = _entete_rapport(f'ÉTAT DU STOCK AU {jour:%d/%m/%Y}',
                               f"{donnees['nb']} article(s) · valorisé au prix moyen pondéré · {regroupement}")
    entetes = ['Code', 'Désignation', 'Unité', 'Quantité', 'Prix moyen', f'Valeur ({p.devise})']
    largeurs = [32 * mm, 70 * mm, 20 * mm, 18 * mm, 18 * mm, 22 * mm]
    groupes = donnees['groupes']
    if not groupes:
        elements.append(Paragraph('Aucun article en stock à cette date.', NORMAL))
    else:
        elements += _recapitulatif('Bloc' if par == 'bloc' else 'Catégorie', groupes, donnees['total'], p.devise)
        for groupe in groupes:
            elements.append(Paragraph(escape(groupe['nom']), GROUPE))
            elements += _tableau(entetes, [
                [l['code'], Paragraph(escape(l['designation']), PETIT), l['unite'], quantite(l['qte']),
                 nombre(l['pm']) + (' *' if l['approx'] else ''), nombre(l['valeur'])] for l in groupe['lignes']
            ], total=nombre(groupe['total']), largeurs=largeurs, droite={3, 4, 5}, libelle_total='Sous-total')
    fin = _total_general(donnees['total'], p.devise) if groupes else []
    if donnees['nb_approx']:
        fin.append(Paragraph(
            '* Prix moyen actuel : le prix moyen à cette date n\'est pas connu (mouvement enregistré avant la '
            'mise à jour du logiciel).', NOTE))
    # Le total général ne part pas seul sur une page sans les signatures (ni les signatures sans lui).
    elements.append(_fin_signee(fin, p.signataire, 'Le responsable'))
    return _rapport(elements, f'etat_stock_au_{jour:%Y-%m-%d}', f'État du stock au {jour:%d/%m/%Y}', utilisateur)


# =========================================================
# Bon de retour de chantier
# =========================================================

from .models import BonRetour  # noqa: E402  (ajouté à la suite des bons existants)


def _bon_retour(bon):
    devise = Parametres.actuels().devise
    elements = _entete(bon, 'BON DE RETOUR DE CHANTIER')
    infos = [
        ('Date', _date(bon)),
        ('Chantier', bon.chantier),
        ('Engin', bon.engin),
        ('Rapporté par', bon.rapporte_par),
    ]
    if bon.sortie_id:
        infos.append(("Bon de sortie d'origine", bon.sortie.numero))
    infos.append(('Observation', bon.observation))
    elements += _infos(infos)
    lignes = [
        [l.article.code, Paragraph(escape(l.article.designation), PETIT), f'{quantite(l.quantite)} {l.article.unite}',
         nombre(l.prix_unitaire), nombre(l.montant)]
        for l in bon.lignes.select_related('article')
    ]
    elements += _tableau(['Code', 'Désignation', 'Quantité rendue', 'P.U.', f'Montant ({devise})'], lignes,
                         total=nombre(bon.total()), largeurs=[25 * mm, 65 * mm, 30 * mm, 25 * mm, 30 * mm])
    elements += _signatures('Rapporté par', Parametres.actuels().signataire)
    return elements


GENERATEURS[BonRetour] = _bon_retour


# =========================================================
# À commander, fiche de stock d'un article
# =========================================================

def rapport_a_commander(donnees, utilisateur=None):
    """donnees : résultat de rapports.a_commander() ; un tableau par fournisseur, cases de signature en bas."""
    p = Parametres.actuels()
    jour = timezone.localdate()
    elements = _entete_rapport('ARTICLES À COMMANDER', (
        f"{donnees['nb']} article(s) au {jour:%d/%m/%Y} · quantité suggérée : de quoi revenir à deux fois le seuil "
        "d'alerte · fournisseur et prix du dernier achat"))
    groupes = donnees['groupes']
    if not groupes:
        elements.append(Paragraph('Aucun article à commander.', NORMAL))
    elif len(groupes) > 1:
        elements += _recapitulatif('Fournisseur', groupes, donnees['total'], p.devise, 'Montant estimé')
    entetes = ['Code', 'Désignation', 'Stock', 'Seuil', 'À commander', 'Unité', 'Dernier prix', f'Montant ({p.devise})']
    largeurs = [28 * mm, 47 * mm, 15 * mm, 15 * mm, 23 * mm, 16 * mm, 17 * mm, 19 * mm]
    for groupe in groupes:
        elements.append(Paragraph(escape(groupe['nom']), GROUPE))
        tableau = _tableau(entetes, [
            [l['code'], Paragraph(escape(l['designation']), PETIT), quantite(l['stock']), quantite(l['stock_min']),
             quantite(l['qte']), l['unite'], nombre(l['prix']) if l['prix'] else '—',
             nombre(l['montant']) if l['montant'] is not None else '—'] for l in groupe['lignes']
        ], total=nombre(groupe['total']), largeurs=largeurs, droite={2, 3, 4, 6, 7}, libelle_total='Sous-total')
        tableau[0].setStyle(TableStyle([('FONTNAME', (4, 1), (4, -1), 'Helvetica-Bold')]))
        elements += tableau
    fin = _total_general(donnees['total'], p.devise) if groupes else []
    if any(l['prix'] is None for g in groupes for l in g['lignes']):
        fin.append(Paragraph('— : aucun achat avec un prix enregistré pour cet article ; le montant n\'est pas estimé.',
                             NOTE))
    elements.append(_fin_signee(fin, p.signataire, 'Le responsable'))
    return _rapport(elements, f'a_commander_{jour:%Y-%m-%d}', f'À commander au {jour:%d/%m/%Y}', utilisateur)


def rapport_fiche_stock(article, fiche, du=None, au=None, utilisateur=None):
    """Fiche de stock (kardex) : fiche = rapports.fiche_stock() ; mouvements du plus ancien au plus récent, stock au
    début de la période, totaux des entrées et des sorties, stock et valeur à la fin."""
    from .rapports import decrire_mouvements, texte_periode

    p = Parametres.actuels()
    periode = texte_periode(du, au)
    elements = _entete_rapport('FICHE DE STOCK', f'{article.code} – {article.designation}')
    elements += _infos([
        ('Article', article.designation), ('Code', article.code), ('Unité', _unite(article)),
        ('Catégorie', article.categorie.nom), ('Emplacement', article.emplacement or 'Non rangé'),
        ("Seuil d'alerte", quantite(article.stock_min)), ('Période', periode),
        ('Prix moyen actuel', f'{nombre(article.prix_moyen)} {p.devise}'),
    ])
    mouvements = decrire_mouvements(fiche['mouvements'].select_related('chantier', 'engin').order_by('date', 'id'))
    italique = ParagraphStyle('Italique', parent=PETIT, fontName='Helvetica-Oblique')
    lignes = []
    if du:
        lignes.append(['', '', Paragraph(f'Stock au {du:%d/%m/%Y}', italique), '', '', '',
                       quantite(fiche['stock_debut']), nombre(fiche['valeur_debut'])])
    for m in mouvements:
        prix = m.prix_moyen_apres if m.prix_moyen_apres is not None else article.prix_moyen
        texte = f'<b>{escape(m.get_type_display())}</b>'
        if m.detail:
            texte += f'<br/>{escape(m.detail)}'
        if m.annule:
            texte += ' <font color="#c9302c">(bon annulé)</font>'
        lignes.append([
            timezone.localtime(m.date).strftime('%d/%m/%Y %H:%M'), m.reference, Paragraph(texte, PETIT),
            quantite(m.quantite) if m.quantite > 0 else '', quantite(-m.quantite) if m.quantite < 0 else '',
            nombre(m.prix_unitaire), quantite(m.stock_apres), nombre(m.stock_apres * prix),
        ])
    droite = {3, 4, 5, 6, 7}
    # Les deux dernières lignes et les totaux forment un tableau à part, aux mêmes largeurs, gardé avec les
    # signatures : ni les totaux ni les signatures ne se retrouvent seuls en haut d'une page.
    queue = lignes[-2:] if len(lignes) > 2 else []
    corps = lignes[:len(lignes) - len(queue)]
    tableau = _tableau(['Date', 'Bon', 'Opération', 'Entrée', 'Sortie', 'Prix unit.', 'Stock', f'Valeur stock ({p.devise})'],
                       corps, largeurs=[21 * mm, 26 * mm, 47 * mm, 15 * mm, 15 * mm, 17 * mm, 16 * mm, 23 * mm],
                       droite=droite)[0]
    elements.append(tableau)
    largeurs = tableau._colWidths
    for ligne in queue:
        ligne[0] = Paragraph('<br/>'.join(escape(m) for m in _replier(ligne[0], largeurs[0] - 2 * MARGE_CELLULE)), CODE)
    totaux = Table(queue + [['', '', Paragraph('<b>Totaux · stock à la fin</b>', PETIT), quantite(fiche['entrees']),
                             quantite(fiche['sorties']), '', quantite(fiche['stock_fin']), nombre(fiche['valeur_fin'])]],
                   colWidths=largeurs)
    fonds = [colors.white, colors.HexColor('#f7f9fb')]
    style = [
        ('FONTSIZE', (0, 0), (-1, -1), 9), ('GRID', (0, 0), (-1, -1), 0.4, TRAIT), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), MARGE_CELLULE), ('RIGHTPADDING', (0, 0), (-1, -1), MARGE_CELLULE),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'), ('BACKGROUND', (0, -1), (-1, -1), GRIS_CLAIR),
    ]
    style += [('ALIGN', (j, 0), (j, -1), 'RIGHT') for j in sorted(droite)]
    if queue:
        decalage = len(corps) % 2
        style.append(('ROWBACKGROUNDS', (0, 0), (-1, len(queue) - 1), fonds[decalage:] + fonds[:decalage]))
    totaux.setStyle(TableStyle(style))
    fin = [totaux, Spacer(1, 4 * mm)]
    if not fiche['nb']:
        fin.append(Paragraph('Aucun mouvement sur cette période.', NOTE))
    elements.append(_fin_signee(fin, p.signataire, 'Le responsable'))
    return _rapport(elements, f'fiche_stock_{re.sub(r"[^A-Za-z0-9_-]+", "-", article.code)[:40]}',
                    f'Fiche de stock {article.code}', utilisateur)


# =========================================================
# Inventaire : fiche de comptage et résultat (écarts)
# =========================================================

from reportlab.pdfgen.canvas import Canvas  # noqa: E402
from reportlab.platypus import KeepTogether  # noqa: E402

from .utils import UNITES_LISIBLES  # noqa: E402

CELLULE = ParagraphStyle('Cellule', parent=NORMAL, fontSize=8.5, leading=10)
CELLULE_PETITE = ParagraphStyle('CellulePetite', parent=CELLULE, fontSize=7.5, leading=9)
SECTION = ParagraphStyle('Section', parent=NORMAL, fontName='Helvetica-Bold', fontSize=11, leading=14,
                         textColor=BLEU, spaceBefore=4, spaceAfter=4, keepWithNext=1)


def _canevas_numerote(texte):
    """Pied de page de chaque feuille : « INV-… · Fiche de comptage » à gauche, « Page 2 / 5 » à droite."""
    class CanevasNumerote(Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._pages = []

        def showPage(self):
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            for etat in self._pages:
                self.__dict__.update(etat)
                self.setFont('Helvetica', 8)
                self.setFillColor(colors.grey)
                self.drawString(15 * mm, 7 * mm, texte)
                self.drawRightString(A4[0] - 15 * mm, 7 * mm, f'Page {self._pageNumber} / {len(self._pages)}')
                super().showPage()
            super().save()
    return CanevasNumerote


def _document_inventaire(elements, nom_fichier, pied):
    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = f'inline; filename="{nom_fichier}.pdf"'
    doc = SimpleDocTemplate(response, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=12 * mm, bottomMargin=15 * mm, title=nom_fichier)
    doc.build(elements, canvasmaker=_canevas_numerote(pied))
    return response


def _unite_courte(article):
    """Unité abrégée pour les colonnes étroites de l'inventaire : « SAC », « m² »."""
    return UNITES_LISIBLES.get(article.unite, article.unite)


def _signe(valeur, texte):
    return f'+{texte}' if valeur > 0 else texte


def _par_etagere(lignes):
    """Lignes triées par emplacement → [(titre de l'étagère, lignes)], les articles non rangés à la fin."""
    groupes = []
    for ligne in lignes:
        etagere = ligne.article.etagere
        titre = (f'Bloc {etagere.bloc.code} › Étagère {etagere.code}' + (f' – {etagere.bloc.nom}' if etagere.bloc.nom else '')
                 if etagere else 'Articles non rangés')
        if not groupes or groupes[-1][0] != titre:
            groupes.append((titre, []))
        groupes[-1][1].append(ligne)
    return groupes


def fiche_comptage(bon, lignes, avec_theorique=False):
    """Fiche à emporter en rayon : une section par étagère, lignes dans l'ordre du rayon, case « Compté » vide.

    lignes : lignes de l'inventaire triées par emplacement. avec_theorique : colonne du stock du logiciel
    (réservée aux responsables ; sans elle, le comptage se fait « à l'aveugle »)."""
    # Pas de mention « BROUILLON » sur une fiche faite pour compter.
    elements = [e for e in _entete(bon, 'FICHE DE COMPTAGE') if getattr(getattr(e, 'style', None), 'name', '') != 'Statut']
    elements += _infos([
        ('À compter', bon.perimetre or 'Inventaire'),
        ('Commencé le', _date(bon)),
        ('Nombre d\'articles', len(lignes)),
        ('Remarque', bon.observation),
    ])
    elements += [Paragraph('Comptez ce qui est réellement en rayon et écrivez la quantité dans la case « Compté » '
                           '(article absent : 0). Article abîmé, mal rangé… : notez-le dans « Remarque ».', PETIT),
                 Spacer(1, 4 * mm)]
    entetes = ['Niv.', 'Code', 'Désignation', 'Unité'] + (['Théorique'] if avec_theorique else []) + ['Compté', 'Remarque']
    largeurs = [12, 28, 50 if avec_theorique else 62, 15] + ([20] if avec_theorique else []) + [25, 30 if avec_theorique else 38]
    compte = entetes.index('Compté')
    for titre, groupe in _par_etagere(lignes):
        donnees = [entetes]
        for l in groupe:
            a = l.article
            designation = escape(a.designation) + (f'<br/><font size="7.5" color="#555555">{escape(a.case)}</font>' if a.case else '')
            donnees.append([str(a.niveau or ''), Paragraph(escape(a.code), CELLULE), Paragraph(designation, CELLULE),
                            _unite_courte(a)] + ([quantite(a.stock)] if avec_theorique else []) + ['', ''])
        table = Table(donnees, colWidths=[x * mm for x in largeurs], repeatRows=1)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), BLEU),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 8.5),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING', (0, 1), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 1), (-1, -1), 6),
            ('ALIGN', (0, 0), (0, -1), 'CENTER'),
            ('ALIGN', (3, 0), (3, -1), 'CENTER'),
            ('BOX', (compte, 0), (compte, -1), 1.2, colors.black),
            ('BACKGROUND', (compte, 1), (compte, -1), colors.HexColor('#fbfcfe')),
        ] + ([('ALIGN', (4, 1), (4, -1), 'RIGHT')] if avec_theorique else [])))
        nb = len(groupe)
        elements += [Paragraph(f'{escape(titre)} <font size="9" color="#555555">({nb} article{"s" if nb > 1 else ""})</font>',
                               SECTION), table, Spacer(1, 5 * mm)]
    if not lignes:
        elements.append(Paragraph('Tous les articles de cet inventaire sont déjà comptés.', NORMAL))
    pied = Table([['Compté par', 'Contrôlé par', 'Date'], ['\n\n\n', '\n\n\n', '\n\n\n']], colWidths=[60 * mm] * 3)
    pied.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('BOX', (0, 0), (0, -1), 0.5, colors.grey),
        ('BOX', (1, 0), (1, -1), 0.5, colors.grey),
        ('BOX', (2, 0), (2, -1), 0.5, colors.grey),
    ]))
    elements.append(KeepTogether([Spacer(1, 4 * mm), Paragraph('Nom et signature :', PETIT), Spacer(1, 2 * mm), pied]))
    return _document_inventaire(elements, f'{bon.numero}-fiche-comptage',
                                f'{bon.numero} · Fiche de comptage · {bon.perimetre or "Inventaire"}')


def resultat_inventaire(bon, lignes):
    """Résultat d'un inventaire validé : écarts en quantité et en valeur, puis les articles non comptés.

    lignes : triées par emplacement, avec .valeur = valeur de l'écart enregistrée à la validation."""
    devise = Parametres.actuels().devise
    ecarts = [l for l in lignes if l.ecart]
    comptees = [l for l in lignes if l.stock_compte is not None]
    non_comptees = [l for l in lignes if l.stock_compte is None]
    total = sum((l.valeur for l in ecarts), 0)
    elements = _entete(bon, "RÉSULTAT D'INVENTAIRE")
    valide = ''
    if bon.date_validation:
        valide = timezone.localtime(bon.date_validation).strftime('%d/%m/%Y %H:%M')
        if bon.valide_par:
            valide += f' par {bon.valide_par.get_full_name() or bon.valide_par.get_username()}'
    elements += _infos([
        ('Compté', bon.perimetre or 'Inventaire'),
        ('Commencé le', _date(bon)),
        ('Validé le', valide),
        ('Articles comptés', f'{len(comptees)} sur {len(lignes)}'),
        ('Écarts corrigés', f'{len(ecarts)} article{"s" if len(ecarts) > 1 else ""} · '
                            f'{_signe(total, nombre(total))} {devise}'),
        ('Remarque', bon.observation),
    ])
    if ecarts:
        donnees = [['Empl.', 'Code', 'Désignation', 'Unité', 'Avant', 'Compté', 'Écart', f'Valeur ({devise})']]
        for l in ecarts:
            a = l.article
            emplacement = ' › '.join(x for x in [a.etagere.bloc.code if a.etagere else '', a.etagere.code if a.etagere else '',
                                                 str(a.niveau or '') if a.etagere else ''] if x) or '—'
            donnees.append([Paragraph(escape(emplacement), CELLULE_PETITE), Paragraph(escape(a.code), CELLULE_PETITE),
                            Paragraph(escape(a.designation), CELLULE), _unite_courte(a), quantite(l.stock_theorique),
                            quantite(l.stock_compte), _signe(l.ecart, quantite(l.ecart)), _signe(l.valeur, nombre(l.valeur))])
        donnees.append(['TOTAL', '', '', '', '', '', '', _signe(total, nombre(total))])
        table = Table(donnees, colWidths=[x * mm for x in [20, 24, 48, 12, 16, 16, 16, 28]], repeatRows=1)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), BLEU),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 8.5),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('ALIGN', (3, 0), (3, -1), 'CENTER'),
            ('ALIGN', (4, 0), (-1, -1), 'RIGHT'),
            ('FONTNAME', (6, 1), (6, -1), 'Helvetica-Bold'),
            ('SPAN', (0, -1), (-2, -1)),
            ('ALIGN', (0, -1), (-1, -1), 'RIGHT'),
            ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
            ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#f2f2f2')),
        ]))
        elements += [Paragraph('Écarts corrigés dans le stock', SECTION), table]
    else:
        elements.append(Paragraph('Aucun écart : le stock du logiciel correspondait au comptage.', NORMAL))
    justes = len(comptees) - len(ecarts)
    if justes:
        elements += [Spacer(1, 3 * mm), Paragraph(f'{justes} article{"s" if justes > 1 else ""} compté{"s" if justes > 1 else ""} '
                                                  'sans écart (ou recompté depuis par un autre inventaire).', PETIT)]
    if non_comptees:
        donnees = [['Code', 'Désignation', 'Emplacement']] + [
            [Paragraph(escape(l.article.code), CELLULE_PETITE), Paragraph(escape(l.article.designation), CELLULE),
             Paragraph(escape(l.article.emplacement or '—'), CELLULE_PETITE)] for l in non_comptees]
        table = Table(donnees, colWidths=[30 * mm, 90 * mm, 60 * mm], repeatRows=1)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f2f2f2')),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 8.5),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        elements += [Spacer(1, 5 * mm), Paragraph(f'Articles non comptés : stock inchangé ({len(non_comptees)})', SECTION), table]
    elements += [Spacer(1, 10 * mm), KeepTogether(_signatures(Parametres.actuels().signataire, 'Le responsable'))]
    return _document_inventaire(elements, f'{bon.numero}-resultat', f"{bon.numero} · Résultat d'inventaire")
