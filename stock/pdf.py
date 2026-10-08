"""Impression des bons (entrée, sortie, inventaire) en PDF."""
from xml.sax.saxutils import escape

from django.http import HttpResponse
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .models import BonEntree, BonSortie, Inventaire, Parametres
from .utils import nombre, quantite

BLEU = colors.HexColor('#1f497d')
STYLES = getSampleStyleSheet()
TITRE = ParagraphStyle('Titre', parent=STYLES['Heading1'], alignment=TA_CENTER, textColor=BLEU)
NORMAL = STYLES['Normal']
PETIT = ParagraphStyle('Petit', parent=NORMAL, fontSize=9)


def _entete(bon, titre):
    """En-tête : logo et coordonnées de la société (Réglages › Ma société), puis titre du bon."""
    p = Parametres.actuels()
    coordonnees = [f'<b>{escape(p.nom_societe)}</b>' if p.nom_societe else '']
    coordonnees += [escape(x) for x in (p.adresse, p.telephone) if x]
    texte = Paragraph('<br/>'.join(c for c in coordonnees if c) or '&nbsp;', ParagraphStyle(
        'Societe', parent=NORMAL, alignment=TA_RIGHT if p.logo else TA_LEFT))
    logo = None
    if p.logo:
        try:
            logo = Image(p.logo.path, width=45 * mm, height=20 * mm, kind='proportional')
            logo.hAlign = 'LEFT'
        except OSError:
            logo = None
    if logo:
        entete = Table([[logo, texte]], colWidths=[90 * mm, 90 * mm])
        entete.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'MIDDLE'), ('LEFTPADDING', (0, 0), (-1, -1), 0)]))
    else:
        entete = texte
    elements = [entete, Spacer(1, 4 * mm), Paragraph(f'{titre} N° {escape(bon.numero)}', TITRE)]
    if bon.statut != bon.VALIDE:
        elements.append(Paragraph(f'<font color="#d9534f"><b>{bon.get_statut_display().upper()}</b></font>',
                                  ParagraphStyle('Statut', parent=NORMAL, alignment=TA_CENTER)))
    elements.append(Spacer(1, 6 * mm))
    return elements


def _infos(lignes):
    table = Table([[Paragraph(f'<b>{k}</b>', NORMAL), Paragraph(escape(str(v or '—')), NORMAL)] for k, v in lignes],
                  colWidths=[45 * mm, 125 * mm], hAlign='LEFT')
    table.setStyle(TableStyle([('BOTTOMPADDING', (0, 0), (-1, -1), 3)]))
    return [table, Spacer(1, 6 * mm)]


def _tableau(entetes, lignes, total=None, largeurs=None):
    data = [entetes] + lignes
    if total is not None:
        data.append(['TOTAL'] + [''] * (len(entetes) - 2) + [total])
    table = Table(data, colWidths=largeurs, repeatRows=1)
    style = [
        ('BACKGROUND', (0, 0), (-1, 0), BLEU),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (2, 1), (-1, -1), 'RIGHT'),
    ]
    if total is not None:
        style += [
            ('SPAN', (0, -1), (-2, -1)),
            ('ALIGN', (0, -1), (-1, -1), 'RIGHT'),
            ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
            ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#f2f2f2')),
        ]
    table.setStyle(TableStyle(style))
    return [table, Spacer(1, 15 * mm)]


def _signatures(gauche, droite):
    table = Table([[gauche, droite], ['\n\n\n', '\n\n\n']], colWidths=[85 * mm, 85 * mm])
    table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('BOX', (0, 0), (0, -1), 0.5, colors.grey),
        ('BOX', (1, 0), (1, -1), 0.5, colors.grey),
    ]))
    return [table]


def _date(bon):
    return timezone.localtime(bon.date).strftime('%d/%m/%Y %H:%M')


def _bon_entree(bon):
    devise = Parametres.actuels().devise
    elements = _entete(bon, "BON D'ENTRÉE")
    elements += _infos([
        ('Date', _date(bon)),
        ('Fournisseur', bon.fournisseur),
        ('Réf. BL / facture', bon.reference),
        ('Observation', bon.observation),
    ])
    lignes = [
        [l.article.code, Paragraph(escape(l.article.designation), PETIT), f'{quantite(l.quantite)} {l.article.unite}',
         nombre(l.prix_unitaire), nombre(l.montant)]
        for l in bon.lignes.select_related('article')
    ]
    elements += _tableau(['Code', 'Désignation', 'Quantité', 'P.U.', f'Montant ({devise})'], lignes,
                         total=nombre(bon.total()), largeurs=[25 * mm, 70 * mm, 25 * mm, 25 * mm, 30 * mm])
    elements += _signatures('Le livreur', Parametres.actuels().signataire)
    return elements


def _bon_sortie(bon):
    devise = Parametres.actuels().devise
    elements = _entete(bon, 'BON DE SORTIE')
    elements += _infos([
        ('Date', _date(bon)),
        ('Chantier', bon.chantier),
        ('Engin', bon.engin),
        ('Demandeur', bon.demandeur),
        ('Observation', bon.observation),
    ])
    lignes = [
        [l.article.code, Paragraph(escape(l.article.designation), PETIT), f'{quantite(l.quantite)} {l.article.unite}',
         nombre(l.prix_unitaire), nombre(l.montant)]
        for l in bon.lignes.select_related('article')
    ]
    elements += _tableau(['Code', 'Désignation', 'Quantité', 'P.U.', f'Montant ({devise})'], lignes,
                         total=nombre(bon.total()), largeurs=[25 * mm, 70 * mm, 25 * mm, 25 * mm, 30 * mm])
    elements += _signatures(Parametres.actuels().signataire, 'Le réceptionnaire')
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


def reponse_pdf(bons, nom_fichier):
    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = f'inline; filename="{nom_fichier}.pdf"'
    doc = SimpleDocTemplate(response, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=12 * mm, bottomMargin=12 * mm, title=nom_fichier)
    elements = []
    for i, bon in enumerate(bons):
        if i:
            elements.append(PageBreak())
        elements += GENERATEURS[type(bon)](bon)
    doc.build(elements)
    return response
