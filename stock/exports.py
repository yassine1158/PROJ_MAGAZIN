"""Exports Excel.

Chaque feuille commence par 3 lignes de titre (société ; titre et filtres ; « Édité le … par … »), puis le
tableau : en-têtes, nombres et dates au format Excel (triables, additionnables), lignes de total en gras,
filtre automatique et mise en page paysage ajustée à la largeur d'une page.
"""
import re
from datetime import date, datetime
from decimal import Decimal

from django.http import HttpResponse
from django.utils import timezone

TEXTE, MONTANT, PRIX, QTE, DATE, DATE_HEURE = 'texte', 'montant', 'prix', 'qte', 'date', 'date_heure'
# Nombres entiers sans décimales (« 12 » et non « 12, »), décimales seulement quand il y en a.
FORMATS = {
    MONTANT: ('#,##0', '#,##0'),
    PRIX: ('#,##0', '#,##0.00'),
    QTE: ('#,##0', '#,##0.###'),
    DATE: ('dd/mm/yyyy', 'dd/mm/yyyy'),
    DATE_HEURE: ('dd/mm/yyyy hh:mm', 'dd/mm/yyyy hh:mm'),
}
LIGNE_ENTETES = 5  # lignes 1 à 3 : titre ; ligne 4 : vide


class Gras(list):
    """Ligne écrite en gras au milieu du tableau (sous-total d'un groupe)."""


def feuille(titre, colonnes, lignes, filtres='', pied=()):
    """Une feuille du classeur.

    colonnes : [(entête, format), …] ou simplement « entête » pour du texte ; format parmi TEXTE, MONTANT
    (FCFA, sans décimale), PRIX, QTE, DATE, DATE_HEURE ;
    lignes : listes de valeurs (Decimal, date et datetime acceptés) ; une ligne Gras([…]) est un sous-total ;
    filtres : précisions ajoutées au titre (période, chantier…) ;
    pied : lignes de total, en gras sous le tableau (hors du filtre automatique).
    """
    return {'titre': titre, 'colonnes': [c if isinstance(c, tuple) else (c, TEXTE) for c in colonnes],
            'lignes': lignes, 'filtres': filtres, 'pied': list(pied)}


def _valeur(v, format_):
    if isinstance(v, datetime):
        # Excel ne connaît pas les fuseaux horaires : heure locale, à la seconde près.
        return (timezone.localtime(v) if timezone.is_aware(v) else v).replace(tzinfo=None, microsecond=0)
    if isinstance(v, Decimal):
        v = float(v)
    if format_ == MONTANT and isinstance(v, float):
        v = round(v)
    return v


def _nom(utilisateur):
    if not utilisateur:
        return ''
    return utilisateur.get_full_name() or utilisateur.get_username()


def _remplir(ws, f, societe, edition):
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    colonnes = f['colonnes']
    nb = len(colonnes)
    derniere = get_column_letter(nb)
    ws.title = re.sub(r'[\\/?*\[\]:]', '-', f['titre'])[:31]
    ws['A1'] = societe
    ws['A1'].font = Font(bold=True, size=14)
    ws['A2'] = f['titre'] + (f' · {f["filtres"]}' if f['filtres'] else '')
    ws['A2'].font = Font(bold=True, size=12, color='1F497D')
    ws['A3'] = edition
    ws['A3'].font = Font(italic=True, size=9, color='5F6B7A')
    for cellule in ('A1', 'A2', 'A3'):
        ws[cellule].data_type = 's'  # jamais une formule, même si le nom commence par « = »

    bleu = PatternFill('solid', fgColor='1F497D')
    for j, (entete, format_) in enumerate(colonnes, start=1):
        cellule = ws.cell(LIGNE_ENTETES, j, entete)
        cellule.font = Font(bold=True, color='FFFFFF')
        cellule.fill = bleu
        cellule.alignment = Alignment(vertical='center', wrap_text=True,
                                      horizontal='right' if format_ in (MONTANT, PRIX, QTE) else 'left')

    gris = PatternFill('solid', fgColor='EEF1F5')
    trait = Border(top=Side(style='thin', color='5F6B7A'))
    largeurs = [len(str(e)) for e, _ in colonnes]
    numero = LIGNE_ENTETES
    for groupe, lignes in (('donnees', f['lignes']), ('pied', f['pied'])):
        if groupe == 'pied':
            fin_donnees = numero
        for ligne in lignes:
            numero += 1
            gras = groupe == 'pied' or isinstance(ligne, Gras)
            for j, (v, (_, format_)) in enumerate(zip(ligne, colonnes), start=1):
                v = _valeur(v, format_)
                if v is None or v == '':
                    cellule = ws.cell(numero, j)
                else:
                    cellule = ws.cell(numero, j, v)
                    if isinstance(v, str) and v[:1] in ('=', '+', '-', '@'):
                        cellule.data_type = 's'  # texte saisi, pas une formule
                        cellule.quotePrefix = True
                    elif isinstance(v, (int, float)) and format_ in FORMATS:
                        cellule.number_format = FORMATS[format_][0 if float(v).is_integer() else 1]
                    elif isinstance(v, (date, datetime)):
                        cellule.number_format = FORMATS[DATE_HEURE if isinstance(v, datetime) else DATE][0]
                        cellule.alignment = Alignment(horizontal='left')  # comme l'en-tête, collée à gauche
                    texte = f'{v:,.0f}' if isinstance(v, (int, float)) else str(v)
                    largeurs[j - 1] = max(largeurs[j - 1], 16 if isinstance(v, datetime) else len(texte))
                if gras:
                    cellule.font = Font(bold=True)
                    cellule.fill = gris
                if groupe == 'pied':
                    cellule.border = trait

    for j, (largeur, (_, format_)) in enumerate(zip(largeurs, colonnes), start=1):
        marge = 4 if format_ in (MONTANT, PRIX, QTE) else 2
        ws.column_dimensions[get_column_letter(j)].width = min(max(largeur + marge, 8), 50)
    ws.row_dimensions[LIGNE_ENTETES].height = 30
    ws.freeze_panes = ws.cell(LIGNE_ENTETES + 1, 1)
    ws.auto_filter.ref = f'A{LIGNE_ENTETES}:{derniere}{max(fin_donnees, LIGNE_ENTETES)}'
    ws.print_title_rows = f'{LIGNE_ENTETES}:{LIGNE_ENTETES}'
    ws.page_setup.orientation = 'landscape'
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_margins.left = ws.page_margins.right = 0.4
    ws.page_margins.top = ws.page_margins.bottom = 0.6
    ws.oddFooter.left.text = f['titre'][:100].replace('&', '&&')
    ws.oddFooter.right.text = 'Page &P / &N'


def classeur(feuilles, nom_fichier, utilisateur=None):
    """Fichier Excel de plusieurs feuilles (voir feuille()), nommé « <nom_fichier>.xlsx »."""
    # Chargé seulement au moment de l'export, pour un démarrage plus rapide.
    from openpyxl import Workbook

    from config import produit

    from .models import Parametres

    maintenant = timezone.localtime()
    edition = f'Édité le {maintenant:%d/%m/%Y} à {maintenant:%H:%M}'
    if utilisateur:
        edition += f' par {_nom(utilisateur)}'
    societe = Parametres.actuels().nom_societe or produit.NOM
    wb = Workbook()
    for i, f in enumerate(feuilles):
        _remplir(wb.active if i == 0 else wb.create_sheet(), f, societe, edition)

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="{nom_fichier}.xlsx"'
    wb.save(response)
    return response


def reponse_excel(titre, colonnes, lignes, nom_fichier, filtres='', pied=(), utilisateur=None, dater=True):
    """Fichier Excel d'une seule feuille, nommé « <nom_fichier>_<date du jour>.xlsx » (sans la date si dater=False)."""
    if dater:
        nom_fichier += timezone.localdate().strftime('_%Y-%m-%d')
    return classeur([feuille(titre, colonnes, lignes, filtres, pied)], nom_fichier, utilisateur)


def _devise():
    from .models import Parametres

    return Parametres.actuels().devise or 'FCFA'


def entete_valeur():
    """« Valeur (FCFA) », avec la devise choisie dans Réglages › Ma société."""
    return f'Valeur ({_devise()})'


def alerte(article):
    if article.actif and article.stock <= 0:
        return 'Rupture'
    return 'À commander' if article.en_alerte else ''


def etat_stock(articles, utilisateur=None, filtres=''):
    articles = list(articles)
    return reponse_excel(
        'État du stock',
        ['Code', 'Désignation', 'Catégorie', 'Unité', 'Emplacement', ('Stock', QTE), ('Stock min', QTE),
         ('Prix moyen', PRIX), (entete_valeur(), MONTANT), 'Alerte'],
        [[a.code, a.designation, a.categorie.nom, a.get_unite_display(), a.emplacement, a.stock, a.stock_min,
          a.prix_moyen, a.valeur_stock, alerte(a)] for a in articles],
        'etat_stock', filtres=filtres, utilisateur=utilisateur,
        pied=[['TOTAL', f'{len(articles)} article(s)'] + [''] * 6 + [sum((a.valeur_stock for a in articles),
                                                                          Decimal('0')), '']],
    )


def mouvements(qs, utilisateur=None, filtres='', nom_fichier=None):
    """Journal des mouvements ; nom_fichier : nom choisi (sinon « mouvements_<date du jour> »).

    Les lignes d'un bon annulé sont marquées « Bon annulé » et ne comptent pas dans les totaux."""
    from .rapports import references_annulees

    mouvements = list(qs.select_related('article', 'chantier', 'engin', 'utilisateur'))
    annules = references_annulees({m.reference for m in mouvements})
    lignes, entrees, sorties = [], Decimal('0'), Decimal('0')
    for m in mouvements:
        valeur = m.quantite * m.prix_unitaire
        annule = m.reference in annules
        if not annule and valeur > 0:
            entrees += valeur
        elif not annule:
            sorties += valeur
        lignes.append([
            m.date, m.reference, m.get_type_display(), m.article.code, m.article.designation, m.quantite,
            m.article.get_unite_display(), m.stock_apres, m.prix_unitaire, valeur,
            m.chantier.nom if m.chantier else '', m.engin.code if m.engin else '', _nom(m.utilisateur),
            'Bon annulé' if annule else '',
        ])
    vide = [''] * 8
    hors = ' (hors bons annulés)' if annules else ''
    return reponse_excel(
        'Mouvements',
        [('Date', DATE_HEURE), 'Bon', 'Type', 'Code', 'Désignation', ('Quantité', QTE), 'Unité',
         ('Stock après', QTE), ('Prix unitaire', PRIX), (entete_valeur(), MONTANT), 'Chantier', 'Engin',
         'Utilisateur', 'Remarque'],
        lignes, nom_fichier or 'mouvements', filtres=filtres, utilisateur=utilisateur, dater=not nom_fichier,
        pied=[[f'Total des entrées{hors}', *vide, entrees, '', '', '', ''],
              [f'Total des sorties{hors}', *vide, sorties, '', '', '', '']],
    )


def _nom_fichier(texte):
    """Morceau de nom de fichier sans caractère gênant (code article saisi librement)."""
    return re.sub(r'[^A-Za-z0-9_-]+', '-', texte).strip('-')[:40] or 'article'


def _jour(moment):
    return timezone.localtime(moment).date() if moment else None


def a_commander(donnees, utilisateur=None):
    """Articles à commander (rapports.a_commander) : quantité suggérée, dernier prix, sous-total par fournisseur."""
    lignes = []
    for g in donnees['groupes']:
        lignes += [[g['nom'], l['code'], l['designation'], l['unite'], l['stock'], l['stock_min'], l['qte'], l['prix'],
                    l['montant'], _jour(l['date'])] for l in g['lignes']]
        lignes.append(Gras([f'Sous-total {g["nom"]}'] + [''] * 7 + [g['total'], '']))
    return reponse_excel(
        'À commander',
        ['Fournisseur', 'Code', 'Désignation', 'Unité', ('Stock', QTE), ("Seuil d'alerte", QTE),
         ('Quantité à commander', QTE), ('Dernier prix', PRIX), (f'Montant estimé ({_devise()})', MONTANT),
         ('Dernier achat', DATE)],
        lignes, 'a_commander', utilisateur=utilisateur,
        filtres=f"{donnees['nb']} article(s) · quantité suggérée = 2 × seuil − stock · fournisseur du dernier achat",
        pied=[['TOTAL', f"{donnees['nb']} article(s)"] + [''] * 6 + [donnees['total'], '']],
    )


def fiche_stock(article, fiche, du=None, au=None, utilisateur=None):
    """Fiche de stock d'un article (rapports.fiche_stock), du plus ancien au plus récent : stock au début, chaque
    mouvement avec son bon, totaux des entrées et des sorties, stock à la fin."""
    from .rapports import decrire_mouvements, texte_periode

    mouvements = decrire_mouvements(fiche['mouvements'].select_related('chantier', 'engin', 'utilisateur')
                                    .order_by('date', 'id'))
    lignes = []
    if du:
        lignes.append(Gras(['', '', f'Stock au {du:%d/%m/%Y}', '', '', '', fiche['stock_debut'], '', '', '',
                            fiche['valeur_debut'], '', '']))
    for m in mouvements:
        prix = m.prix_moyen_apres if m.prix_moyen_apres is not None else article.prix_moyen
        lignes.append([
            m.date, m.reference, m.get_type_display(), m.detail, m.quantite if m.quantite > 0 else '',
            -m.quantite if m.quantite < 0 else '', m.stock_apres, m.prix_unitaire, abs(m.quantite * m.prix_unitaire),
            m.prix_moyen_apres, m.stock_apres * prix, _nom(m.utilisateur), 'Bon annulé' if m.annule else '',
        ])
    filtres = ' · '.join([f'{article.code} – {article.designation}', f'Unité : {article.get_unite_display()}',
                          texte_periode(du, au)])
    return reponse_excel(
        'Fiche de stock',
        [('Date', DATE_HEURE), 'Bon', 'Opération', 'Fournisseur / chantier', ('Entrée', QTE), ('Sortie', QTE),
         ('Stock après', QTE), ('Prix unitaire', PRIX), (entete_valeur(), MONTANT), ('Prix moyen après', PRIX),
         (f'Valeur du stock ({_devise()})', MONTANT), 'Utilisateur', 'Remarque'],
        lignes, f'fiche_stock_{_nom_fichier(article.code)}', filtres=filtres, utilisateur=utilisateur,
        pied=[['TOTAL', '', f"{fiche['nb']} mouvement(s)", 'Stock à la fin', fiche['entrees'], fiche['sorties'],
               fiche['stock_fin'], '', '', '', fiche['valeur_fin'], '', '']],
    )
