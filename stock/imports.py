"""Import des articles depuis un fichier Excel (.xlsx) ou CSV.

lire() lit le fichier, analyser() vérifie chaque ligne sans rien écrire en base (aperçu), importer() crée en une
transaction les catégories, les blocs et étagères manquants (si demandé), les articles et un seul bon d'entrée
« Stock de départ » validé : le prix moyen et l'historique restent justes.
"""
import csv
import io
import json
import math
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone

from . import services
from .models import Article, Bloc, BonEntree, Categorie, Etagere, LigneEntree

MAX_OCTETS = 10 * 1024 * 1024
MAX_LIGNES = 5000
QTE_MAXI = Decimal('1000000000')
PRIX_MAXI = Decimal('10000000000')
NIVEAU_MAXI = 20
CATEGORIE_DEFAUT = 'Divers'
OK, AVERTISSEMENT, ERREUR = 'ok', 'avertissement', 'erreur'


class ImportImpossible(Exception):
    """Fichier illisible ou sans articles : rien n'est importé (message clair pour l'utilisateur)."""


@dataclass(frozen=True)
class Colonne:
    cle: str
    titre: str
    aide: str
    exemple: str
    autres_titres: tuple = ()  # titres reconnus, écrits sans accents ni majuscules
    obligatoire: bool = False
    dans_modele: bool = True


COLONNES = (
    Colonne('code', 'Code', "Code de l'article (lettres, chiffres, tirets). Vide : le code est créé automatiquement "
            "à partir de la catégorie (ex. MAT-0001).", 'CIM-CPJ45',
            ('ref', 'reference', 'code article', 'ref article', 'reference article', 'code produit', 'n article',
             'numero article')),
    Colonne('designation', 'Désignation', "Nom de l'article, tel qu'il sera affiché et imprimé sur les bons.",
            'Ciment CPJ 45 (sac 50 kg)',
            ('libelle', 'nom', 'article', 'description', 'produit', 'nom article', 'nom de l article',
             'designation article', 'libelle article', 'intitule'), obligatoire=True),
    Colonne('categorie', 'Catégorie', "Famille de l'article (Filtres, Matériaux, EPI…). Une catégorie qui n'existe "
            "pas encore est créée. Vide : « Divers ».", 'Matériaux',
            ('famille', 'groupe', 'rayon', 'type', 'type article', 'categorie article')),
    Colonne('unite', 'Unité', 'Unité de stock : voir la liste des unités plus bas. Vide : U (unité).', 'SAC',
            ('u', 'udm', 'um', 'unite de mesure', 'unite de stock')),
    Colonne('stock', 'Stock de départ', "Quantité présente aujourd'hui au magasin. Toutes ces quantités entrent en "
            "stock par un seul bon « Stock de départ ». Vide : 0.", '400',
            ('stock', 'quantite', 'qte', 'qty', 'stock actuel', 'stock initial', 'quantite en stock', 'qte en stock',
             'qte stock', 'quantite stock', 'stock reel', 'stock physique')),
    Colonne('prix', 'Prix unitaire', "Prix d'achat d'une unité, sans « F » ni « FCFA » (ils sont acceptés quand même). "
            'Sert à calculer la valeur du stock.', '5200',
            ('prix', 'pu', 'p u', 'prix achat', 'prix d achat', 'prix d achat unitaire', 'prix unitaire d achat',
             'cout', 'cout unitaire', 'prix moyen', 'pmp', 'cump', 'prix de revient')),
    Colonne('emplacement', 'Emplacement', 'Bloc-Étagère-Niveau, ex. D-M1-1 : bloc D, étagère M1, niveau 1 (en bas). '
            'Vide : article non rangé. À la place, vous pouvez mettre trois colonnes Bloc, Étagère et Niveau.',
            'D-M1-1',
            ('localisation', 'adresse', 'place', 'rangement', 'casier', 'position', 'lieu', 'adresse de stockage')),
    Colonne('bloc', 'Bloc', 'À la place de la colonne Emplacement : code du bloc (A, B…).', 'D',
            ('zone', 'allee'), dans_modele=False),
    Colonne('etagere', 'Étagère', "À la place de la colonne Emplacement : code de l'étagère (R1, M2…).", 'M1',
            ('rayonnage', 'rack'), dans_modele=False),
    Colonne('niveau', 'Niveau', 'À la place de la colonne Emplacement : niveau, 1 = en bas.', '1',
            ('niv', 'etage'), dans_modele=False),
    Colonne('stock_min', 'Stock minimum', "Seuil d'alerte : quand le stock descend à ce chiffre, l'article passe "
            "« À commander ». Vide : 0 (pas d'alerte).", '100',
            ('stock min', 'stock mini', 'seuil', 'seuil d alerte', 'seuil alerte', 'minimum', 'mini',
             'stock de securite', 'stock securite', 'point de commande')),
    Colonne('mots_cles', 'Mots-clés', 'Autres noms utilisés au magasin, séparés par des virgules '
            '(ex. sac de ciment, ciment 45).', 'ciment, sac de ciment',
            ('mots cles', 'mot cle', 'mots cle', 'autres noms', 'autre nom', 'synonymes', 'alias', 'noms usuels')),
    Colonne('reference_fabricant', 'Référence fabricant', 'Référence du fabricant ou du fournisseur (facultatif).',
            'CPJ45-50', ('ref fabricant', 'reference constructeur', 'ref constructeur', 'reference fournisseur',
                         'ref fournisseur', 'part number', 'pn', 'oem', 'reference origine', 'ref origine')),
)
COLONNES_MODELE = [c for c in COLONNES if c.dans_modele]

# Unités écrites autrement (sans accents, en minuscules). Une unité absente de Article.UNITES devient « U ».
SYNONYMES_UNITES = {
    'U': ('u', 'un', 'une', 'unite', 'piece', 'pc', 'pcs', 'pce', 'pces', 'p', 'nb', 'nbre', 'ea', 'unit', 'ex',
          'exemplaire'),
    'KG': ('kg', 'kgs', 'kilo', 'kilogramme'),
    'T': ('t', 'tonne', 'tn'),
    'L': ('l', 'litre', 'lt', 'lts', 'ltr'),
    'M': ('m', 'ml', 'metre', 'metre lineaire', 'mlin'),
    'M2': ('m2', 'metre carre'),
    'M3': ('m3', 'metre cube'),
    'SAC': ('sac',),
    'FUT': ('fut',),
    'BID': ('bid', 'bidon'),
    'BOITE': ('boite', 'bte', 'bt'),
    'JEU': ('jeu', 'jeux', 'kit'),
    'ROUL': ('roul', 'rouleau', 'rouleaux', 'rlx', 'rl'),
    'PAIRE': ('paire', 'pr'),
    'CARTON': ('carton', 'ctn', 'crt', 'cart'),
    'PAQ': ('paq', 'paquet', 'pqt', 'pq', 'sachet'),
    'BARRE': ('barre', 'bar', 'brr'),
    'LOT': ('lot',),
    'FEUILLE': ('feuille', 'tole', 'fle', 'plaque'),
}
VALEURS_VIDES = {'', 'na', 'n a', 'nc', 'neant', 'aucun', 'aucune', 'rien'}  # « - », « N/A », « ? »…
COULEURS_BLOCS = ['#2563eb', '#16a34a', '#d97706', '#9333ea', '#dc2626', '#0891b2', '#db2777', '#65a30d']


def _cle(texte):
    """« Désignation* » → « designation » ; « Prix d'achat (FCFA) » → « prix d achat »."""
    texte = unicodedata.normalize('NFKD', str(texte or '')).encode('ascii', 'ignore').decode().lower()
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', texte).split())


def _cle_titre(texte):
    texte = _cle(re.sub(r'\(.*?\)', ' ', str(texte or '')))
    return re.sub(r'( (fcfa|cfa|xof|ht|ttc|f))+$', '', texte)


TITRES = {}
for _c in COLONNES:
    for _titre in (_cle_titre(_c.titre),) + _c.autres_titres:
        TITRES.setdefault(_titre, _c)


def _texte(valeur):
    """Valeur d'une cellule en texte, espaces en trop retirés (12.0 → « 12 »)."""
    if valeur is None or isinstance(valeur, bool):
        return ''
    if isinstance(valeur, float):
        return str(int(valeur)) if valeur.is_integer() else repr(valeur)
    if hasattr(valeur, 'strftime'):
        return valeur.strftime('%d/%m/%Y')
    return ' '.join(str(valeur).split())


def _nombre(valeur, decimales):
    """Nombre d'une cellule : 12, 12.5, « 1 250 », « 12,5 », « 3 500 F », « 1.250,50 ». None si vide.

    ValueError si ce n'est pas un nombre."""
    if isinstance(valeur, bool):
        raise ValueError
    if valeur is None:
        return None
    if isinstance(valeur, (int, float)):
        if isinstance(valeur, float) and not math.isfinite(valeur):
            raise ValueError
        nombre = Decimal(repr(valeur)) if isinstance(valeur, float) else Decimal(valeur)
    else:
        texte = _texte(valeur)
        if _cle(texte) in VALEURS_VIDES:
            return None
        texte = re.sub(r'(?i)\s*(f\s*cfa|cfa|xof|f)\.?\s*$', '', texte)
        texte = re.sub(r"[\s']", '', texte)
        if ',' in texte and '.' in texte:  # « 1.250,50 » ou « 1,250.50 »
            texte = texte.replace('.', '').replace(',', '.') if texte.rfind(',') > texte.rfind('.') \
                else texte.replace(',', '')
        texte = texte.replace(',', '.')
        if not re.fullmatch(r'[+-]?(\d+\.?\d*|\.\d+)', texte):
            raise ValueError
        try:
            nombre = Decimal(texte)
        except InvalidOperation:
            raise ValueError
    return nombre.quantize(Decimal(1).scaleb(-decimales), ROUND_HALF_UP)


def _tri(code):
    """Ordre naturel : R2 avant R10."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', code)]


def _table_unites():
    connues = dict(Article.UNITES)
    table = {}
    for code, noms in SYNONYMES_UNITES.items():
        for nom in noms:
            table.setdefault(nom, code)
    for code, nom in connues.items():
        table[_cle(code)] = code
        table[_cle(nom)] = code
    return connues, table


def unites_acceptees():
    """[(code, nom, autres écritures acceptées)] pour la feuille d'aide du modèle et l'écran d'import."""
    connues, _ = _table_unites()
    return [(code, nom, ', '.join(n for n in SYNONYMES_UNITES.get(code, ()) if n != code.lower()))
            for code, nom in connues.items()]


# =========================================================
# Lecture du fichier
# =========================================================

def lire(fichier, nom):
    """Lignes d'un fichier .xlsx ou .csv : (nom de la feuille, [(numéro de ligne, [valeurs des cellules])]).

    fichier : chemin ou fichier ouvert (binaire) ; nom : nom d'origine (pour l'extension)."""
    if isinstance(fichier, (str, Path)):
        donnees = Path(fichier).read_bytes()
    else:
        fichier.seek(0)
        donnees = fichier.read()
    extension = Path(nom or '').suffix.lower()
    if donnees[:8] == b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' or extension == '.xls':
        raise ImportImpossible('Ce fichier est au format Excel ancien (.xls). Ouvrez-le dans Excel, puis « Fichier › '
                               'Enregistrer sous » et choisissez « Classeur Excel (.xlsx) ».')
    if not donnees.strip():
        raise ImportImpossible('Ce fichier est vide.')
    if donnees[:2] == b'PK':
        if extension == '.ods':
            raise ImportImpossible('Ce fichier vient de LibreOffice (.ods) : enregistrez-le au format Excel (.xlsx).')
        return _lire_xlsx(donnees)
    if extension in ('.csv', '.txt'):
        return 'CSV', _lire_csv(donnees)
    raise ImportImpossible('Choisissez un fichier Excel (.xlsx) ou CSV (.csv).')


def _lire_xlsx(donnees):
    from openpyxl import load_workbook

    try:
        classeur = load_workbook(io.BytesIO(donnees), read_only=True, data_only=True)
    except Exception:
        raise ImportImpossible("Ce fichier Excel n'a pas pu être lu. Ouvrez-le dans Excel et enregistrez-le de "
                               'nouveau au format .xlsx.')
    try:
        premiere = None
        for feuille in classeur.worksheets:
            if _cle(feuille.title) == 'aide':
                continue
            lignes = []
            vides = 0
            for numero, valeurs in enumerate(feuille.iter_rows(values_only=True), start=1):
                if any(v not in (None, '') for v in valeurs):
                    lignes.append((numero, list(valeurs)))
                    vides = 0
                else:
                    vides += 1
                    if vides > 500 or len(lignes) > MAX_LIGNES + 20:  # mise en forme jusqu'en bas de la feuille
                        break
            if premiere is None:
                premiere = (feuille.title, lignes)
            if _entete(lignes):
                return feuille.title, lignes
        return premiere or ('', [])
    finally:
        classeur.close()


def _lire_csv(donnees):
    for encodage in ('utf-8-sig', 'cp1252', 'latin-1'):
        try:
            texte = donnees.decode(encodage)
            break
        except UnicodeDecodeError:
            continue
    premiere = next((l for l in texte.splitlines() if l.strip()), '')
    separateur = max(';,\t', key=premiere.count) if any(s in premiere for s in ';,\t') else ';'
    lignes = []
    for numero, valeurs in enumerate(csv.reader(io.StringIO(texte, newline=''), delimiter=separateur), start=1):
        if any(v.strip() for v in valeurs):
            lignes.append((numero, valeurs))
        if len(lignes) > MAX_LIGNES + 20:
            break
    return lignes


def _entete(lignes):
    """(position de la ligne des titres, {n° de colonne: Colonne}) parmi les 15 premières lignes ; None sans
    colonne Désignation. Quand deux titres donnent la même colonne, le titre exact l'emporte, sinon le premier."""
    meilleur = None
    for position, (_, valeurs) in enumerate(lignes[:15]):
        choix = {}
        for j, valeur in enumerate(valeurs):
            titre = _cle_titre(valeur)
            colonne = TITRES.get(titre)
            if colonne:
                rang = (0 if titre == _cle_titre(colonne.titre) else 1, j)
                if colonne.cle not in choix or rang < choix[colonne.cle][0]:
                    choix[colonne.cle] = (rang, j, colonne)
        if 'designation' in choix and (meilleur is None or len(choix) > len(meilleur[1])):
            meilleur = (position, {j: colonne for _, j, colonne in choix.values()})
    return meilleur


# =========================================================
# Analyse (aperçu) : rien n'est écrit en base
# =========================================================

@dataclass
class Ligne:
    numero: int
    statut: str = OK
    messages: list = field(default_factory=list)
    code: str = ''
    code_auto: bool = False
    designation: str = ''
    categorie: str = CATEGORIE_DEFAUT
    unite: str = 'U'
    unite_lue: str = ''
    stock: Decimal = None
    prix: Decimal = None
    stock_min: Decimal = Decimal('0')
    mots_cles: str = ''
    reference_fabricant: str = ''
    bloc: str = ''
    etagere: str = ''
    niveau: int = None
    case: str = ''
    nouvel_emplacement: bool = False
    sans_categorie: bool = False

    def avertir(self, message):
        self.messages.append(message)
        if self.statut == OK:
            self.statut = AVERTISSEMENT

    def refuser(self, message):
        self.messages.append(message)
        self.statut = ERREUR

    @property
    def emplacement(self):
        if not self.etagere:
            return ''
        return f'{self.bloc}-{self.etagere}' + (f'-{self.niveau}' if self.niveau else '')

    @property
    def valeur(self):
        return (self.stock or 0) * (self.prix or 0)


@dataclass
class Analyse:
    nom_fichier: str
    feuille: str
    colonnes: list           # [(titre lu dans le fichier, Colonne ou None si ignorée, titre différent ?)]
    lignes: list
    nouvelles_categories: list
    manquants: dict          # {code du bloc: {'existe': bool, 'etageres': {code: niveau le plus haut}}}
    sans_categorie: int      # articles rangés dans « Divers »

    def _avec(self, statut):
        return [l for l in self.lignes if l.statut == statut]

    @property
    def prets(self):
        return self._avec(OK)

    @property
    def avertissements(self):
        return self._avec(AVERTISSEMENT)

    @property
    def erreurs(self):
        return self._avec(ERREUR)

    @property
    def importables(self):
        return [l for l in self.lignes if l.statut != ERREUR]

    @property
    def avec_stock(self):
        return [l for l in self.importables if l.stock]

    @property
    def valeur_stock(self):
        return sum((l.valeur for l in self.avec_stock), Decimal('0'))

    @property
    def nb_etageres_manquantes(self):
        return sum(len(b['etageres']) for b in self.manquants.values())

    @property
    def nb_blocs_manquants(self):
        return sum(1 for b in self.manquants.values() if not b['existe'])

    @property
    def nb_a_ranger(self):
        return sum(1 for l in self.importables if l.nouvel_emplacement)

    def manquants_lisibles(self):
        """[(« Bloc A (nouveau) », ['R1', 'R2', 'R10'])] pour l'aperçu."""
        return [(f'Bloc {code}' + ('' if infos['existe'] else ' (nouveau)'), sorted(infos['etageres'], key=_tri))
                for code, infos in sorted(self.manquants.items(), key=lambda kv: _tri(kv[0]))]


PREFIXES = (r'^bloc\s+', r'^([ée]tag[èe]re|[ée]tag\.?)\s+', r'^(niveau|niv\.?)\s*')
MOTS_EMPLACEMENT = {'bloc', 'etagere', 'etag', 'niveau', 'niv'}


def _decouper_emplacement(texte):
    """« A-R1-2 », « A/R1/2 », « A R1 2 », « Bloc A › Étagère R1 › Niveau 2 › case 3 » → [bloc, étagère, niveau, …]."""
    if re.search(r'[›>]', texte):
        parties = [p.strip() for p in re.split(r'[›>]', texte) if p.strip()]
    else:
        parties = [p for p in re.split(r'[\s/\\;|_-]+', texte) if p and _cle(p) not in MOTS_EMPLACEMENT]
    return [re.sub(PREFIXES[i], '', p, flags=re.I).strip() if i < 3 else p for i, p in enumerate(parties)]


def _niveau(texte, ligne):
    if not texte:
        return None
    chiffres = re.fullmatch(r'(?i)(n|niv|niveau)?\.?\s*(\d+)', texte.strip())
    if not chiffres:
        ligne.avertir(f'Niveau « {texte} » illisible : niveau laissé vide.')
        return None
    niveau = int(chiffres.group(2))
    if not 1 <= niveau <= NIVEAU_MAXI:
        ligne.avertir(f'Niveau {niveau} : entre 1 et {NIVEAU_MAXI}. Niveau laissé vide.')
        return None
    return niveau


class _Plan:
    """Blocs et étagères existants, pour placer les articles (sans tenir compte des majuscules)."""

    def __init__(self):
        self.blocs = {b.code.lower(): b for b in Bloc.objects.all()}
        self.etageres = {}
        self.par_code = {}
        for e in Etagere.objects.select_related('bloc'):
            self.etageres[(e.bloc.code.lower(), e.code.lower())] = e
            self.par_code.setdefault(e.code.lower(), []).append(e)

    def ranger(self, ligne, bloc, etagere, niveau, case):
        bloc, etagere = bloc.strip(), etagere.strip()
        if not bloc and not etagere:
            return
        if etagere and not bloc:
            seules = self.par_code.get(etagere.lower(), [])
            if len(seules) != 1:
                ligne.avertir(f'Étagère « {etagere} » sans bloc : article non rangé.')
                return
            bloc = seules[0].bloc.code
        if not etagere:
            ligne.avertir(f"Emplacement « {bloc} » incomplet (il manque l'étagère) : article non rangé.")
            return
        if len(bloc) > 10 or len(etagere) > 20:
            ligne.avertir('Emplacement trop long (bloc : 10 caractères, étagère : 20) : article non rangé.')
            return
        existante = self.etageres.get((bloc.lower(), etagere.lower()))
        if existante:
            ligne.bloc, ligne.etagere = existante.bloc.code, existante.code
            if niveau and niveau > existante.nb_niveaux:
                ligne.avertir(f"Niveau {niveau} : l'étagère {existante} n'a que {existante.nb_niveaux} niveaux. "
                              'Niveau laissé vide.')
                niveau = None
        else:
            connu = self.blocs.get(bloc.lower())
            ligne.bloc, ligne.etagere = (connu.code if connu else bloc.upper()), etagere.upper()
            ligne.nouvel_emplacement = True
        ligne.niveau = niveau
        ligne.case = case[:30]

    def chercher_etagere(self, bloc, parties):
        """« A-R-1-2 » : l'étagère « R-1 » existe-t-elle dans le bloc A ? → (étagère, reste) sinon None."""
        for n in range(len(parties), 1, -1):
            code = '-'.join(parties[:n])
            if (bloc.lower(), code.lower()) in self.etageres:
                return code, parties[n:]
        return None


def analyser(lignes_lues, nom_fichier='', feuille=''):
    """Vérifie chaque ligne du fichier (voir lire()) et prépare l'import, sans rien écrire en base."""
    entete = _entete(lignes_lues)
    if not entete:
        raise ImportImpossible('Colonne « Désignation » introuvable : la première ligne du fichier doit contenir les '
                               'titres des colonnes (Code, Désignation, Catégorie…). Partez du modèle à télécharger.')
    position, colonnes = entete
    titres = lignes_lues[position][1]
    donnees = []
    for numero, valeurs in lignes_lues[position + 1:]:
        cellules = {c.cle: valeurs[j] for j, c in colonnes.items() if j < len(valeurs)}
        if any(_texte(v) for v in cellules.values()):
            donnees.append((numero, cellules))
    if not donnees:
        raise ImportImpossible("Aucun article dans ce fichier : remplissez au moins la colonne Désignation, sous la "
                               'ligne des titres.')
    if len(donnees) > MAX_LIGNES:
        raise ImportImpossible(f'Ce fichier contient {len(donnees)} lignes : {MAX_LIGNES} au plus par import. '
                               'Découpez-le en plusieurs fichiers.')
    connues, table_unites = _table_unites()
    existants = {}
    designations = {}
    for code, designation in Article.objects.values_list('code', 'designation'):
        existants[code.lower()] = (code, designation)
        designations.setdefault(designation.lower(), code)
    categories = {nom.lower(): nom for nom in Categorie.objects.values_list('nom', flat=True)}
    plan = _Plan()
    codes_fichier, designations_fichier = {}, {}

    lignes = []
    for numero, cellules in donnees:
        texte = {cle: _texte(v) for cle, v in cellules.items()}
        l = Ligne(numero)
        lignes.append(l)
        l.code, l.designation = texte.get('code', ''), texte.get('designation', '')
        if l.code.upper() in ('TOTAL', 'SOUS-TOTAL') and re.match(r'(\d+\s+article|$)', l.designation):
            l.refuser('Ligne de total : ignorée.')
            continue
        if not l.designation:
            l.refuser('Désignation vide : ligne ignorée.')
        elif len(l.designation) > 200:
            l.designation = l.designation[:200]
            l.avertir('Désignation trop longue : coupée à 200 caractères.')
        if len(l.code) > 50:
            l.refuser(f'Code trop long ({len(l.code)} caractères, 50 au plus).')
        elif l.code:
            cle = l.code.lower()
            if cle in existants:
                code, designation = existants[cle]
                l.refuser(f'Le code {code} existe déjà dans le logiciel ({designation}) : ligne ignorée.')
            elif cle in codes_fichier:
                l.refuser(f'Code {l.code} déjà utilisé ligne {codes_fichier[cle]} du fichier : ligne ignorée.')
            else:
                codes_fichier[cle] = numero
        if l.designation and l.statut != ERREUR:
            cle = l.designation.lower()
            if cle in designations:
                if not l.code:
                    l.refuser(f'« {l.designation} » existe déjà dans le logiciel (code {designations[cle]}) : '
                              'ligne ignorée. Donnez-lui un code pour le créer quand même.')
                else:
                    l.avertir(f'Un article « {l.designation} » existe déjà (code {designations[cle]}).')
            elif cle in designations_fichier:
                l.avertir(f'Même désignation que la ligne {designations_fichier[cle]}.')
            else:
                designations_fichier[cle] = numero

        categorie = texte.get('categorie', '')[:100]
        if categorie:
            l.categorie = categories.get(categorie.lower(), categorie)
        else:
            l.categorie = categories.get(CATEGORIE_DEFAUT.lower(), CATEGORIE_DEFAUT)
            l.sans_categorie = True

        l.unite_lue = texte.get('unite', '')
        cle_unite = _cle(l.unite_lue)
        if cle_unite:
            code = table_unites.get(cle_unite)
            if not code and cle_unite.endswith('s'):  # pluriel : « sacs », « barres »
                code = table_unites.get(cle_unite[:-1])
            if code in connues:
                l.unite = code
            else:
                l.avertir(f'Unité « {l.unite_lue} » inconnue : U (unité) utilisée.')

        for cle, decimales, maxi, libelle in (('stock', 3, QTE_MAXI, 'Stock de départ'),
                                              ('prix', 2, PRIX_MAXI, 'Prix unitaire'),
                                              ('stock_min', 3, QTE_MAXI, 'Stock minimum')):
            try:
                valeur = _nombre(cellules.get(cle), decimales)
            except ValueError:
                l.refuser(f'{libelle} : « {texte.get(cle)} » n\'est pas un nombre.')
                continue
            if valeur is None:
                continue
            if valeur < 0:
                l.refuser(f'{libelle} négatif ({texte.get(cle)}).')
            elif valeur > maxi:
                l.refuser(f'{libelle} trop grand ({texte.get(cle)}).')
            else:
                setattr(l, cle, valeur)

        for cle, longueur, libelle in (('mots_cles', 255, 'Mots-clés'),
                                       ('reference_fabricant', 100, 'Référence fabricant')):
            valeur = texte.get(cle, '')
            if len(valeur) > longueur:
                l.avertir(f'{libelle} trop long : coupé à {longueur} caractères.')
            setattr(l, cle, valeur[:longueur])

        if texte.get('emplacement'):
            parties = _decouper_emplacement(texte['emplacement'])
            bloc = parties[0] if parties else ''
            reste = parties[1:]
            trouve = plan.chercher_etagere(bloc, reste) if '-' in texte['emplacement'] else None
            etagere, reste = trouve if trouve else ((reste[0], reste[1:]) if reste else ('', []))
            niveau = reste[0] if reste else ''
            case = ' '.join(reste[1:])
        else:
            bloc, etagere, niveau = (re.sub(PREFIXES[i], '', texte.get(cle, ''), flags=re.I)
                                     for i, cle in enumerate(('bloc', 'etagere', 'niveau')))
            case = ''
        if bloc or etagere:
            plan.ranger(l, bloc, etagere, _niveau(niveau, l), case)

    # Codes automatiques, une fois réservés tous les codes écrits dans le fichier.
    pris = set(existants) | set(codes_fichier)
    compteurs = {}
    for l in lignes:
        if l.statut == ERREUR or l.code:
            continue
        lettres = ''.join(c for c in unicodedata.normalize('NFKD', l.categorie) if c.isascii() and c.isalpha())
        prefixe = lettres[:3].upper() or 'ART'
        if prefixe not in compteurs:
            motif = re.compile(rf'{re.escape(prefixe.lower())}-(\d+)')
            compteurs[prefixe] = max((int(m.group(1)) for c in pris if (m := motif.fullmatch(c))), default=0)
        n = compteurs[prefixe] + 1
        while f'{prefixe}-{n:04d}'.lower() in pris:
            n += 1
        compteurs[prefixe] = n
        l.code, l.code_auto = f'{prefixe}-{n:04d}', True
        pris.add(l.code.lower())

    importables = [l for l in lignes if l.statut != ERREUR]
    nouvelles = {}
    manquants = {}
    for l in importables:
        if l.categorie.lower() not in categories:
            nouvelles.setdefault(l.categorie.lower(), l.categorie)
        if l.nouvel_emplacement:
            bloc = manquants.setdefault(l.bloc, {'existe': l.bloc.lower() in plan.blocs, 'etageres': {}})
            bloc['etageres'][l.etagere] = max(bloc['etageres'].get(l.etagere, 1), l.niveau or 1)
    return Analyse(
        nom_fichier=nom_fichier, feuille=feuille,
        colonnes=[(_texte(t), colonnes.get(j), j in colonnes and _cle_titre(t) != _cle_titre(colonnes[j].titre))
                  for j, t in enumerate(titres) if _texte(t)],
        lignes=lignes, nouvelles_categories=sorted(nouvelles.values(), key=str.lower), manquants=manquants,
        sans_categorie=sum(1 for l in importables if l.sans_categorie),
    )


# =========================================================
# Import : une seule transaction
# =========================================================

@dataclass
class Resultat:
    crees: int
    ignores: int
    bon: BonEntree = None
    nb_lignes_bon: int = 0
    valeur_stock: Decimal = Decimal('0')
    categories: list = field(default_factory=list)
    blocs: list = field(default_factory=list)
    etageres: list = field(default_factory=list)
    non_ranges: int = 0
    duree: float = 0.0


def _creer_emplacements(manquants):
    """Crée les blocs et étagères cités par le fichier, placés automatiquement (à ajuster dans l'éditeur du plan) :
    un nouveau bloc à droite des autres, les étagères en rangées de 2 m, derrière celles qui existent déjà."""
    largeur, profondeur, ecart, allee = Decimal('2'), Decimal('0.9'), Decimal('0.2'), Decimal('1.6')
    blocs = {b.code.lower(): b for b in Bloc.objects.prefetch_related('etageres')}
    x_libre = max((b.x + b.largeur + 2 for b in blocs.values()), default=Decimal('0'))
    blocs_crees, etageres_creees = [], []
    for code_bloc, infos in sorted(manquants.items(), key=lambda kv: _tri(kv[0])):
        codes = sorted(infos['etageres'], key=_tri)
        bloc = blocs.get(code_bloc.lower())
        if bloc:
            existantes = list(bloc.etageres.all())
            z0 = max((e.z + (e.largeur if e.tournee else e.profondeur) + allee for e in existantes), default=Decimal(1))
            par_rangee = max(1, int((bloc.largeur - 2 + ecart) // (largeur + ecart)))
        else:
            par_rangee = min(len(codes), 6)
            rangees = math.ceil(len(codes) / par_rangee)
            bloc = Bloc.objects.create(
                code=code_bloc, couleur=COULEURS_BLOCS[(len(blocs) + len(blocs_crees)) % len(COULEURS_BLOCS)],
                x=x_libre, z=0, largeur=max(Decimal(10), 2 + par_rangee * (largeur + ecart) - ecart),
                profondeur=max(Decimal(8), 2 + rangees * (profondeur + allee) - allee))
            x_libre += bloc.largeur + 2
            z0 = Decimal(1)
            blocs_crees.append(bloc.code)
        for k, code in enumerate(codes):
            etageres_creees.append(Etagere.objects.create(
                bloc=bloc, code=code, x=1 + (k % par_rangee) * (largeur + ecart),
                z=z0 + (k // par_rangee) * (profondeur + allee), largeur=largeur, profondeur=profondeur,
                hauteur=Decimal('2.4'), nb_niveaux=max(4, infos['etageres'][code])))
        fond = z0 + (math.ceil(len(codes) / par_rangee) - 1) * (profondeur + allee) + profondeur + 1
        if fond > bloc.profondeur:
            bloc.profondeur = fond
            bloc.save(update_fields=['profondeur'])
    return blocs_crees, etageres_creees


def importer(analyse, utilisateur, creer_emplacements=True):
    """Crée les articles des lignes sans erreur, en une transaction : tout ou rien."""
    debut = time.perf_counter()
    lignes = analyse.importables
    resultat = Resultat(crees=len(lignes), ignores=len(analyse.erreurs))
    with transaction.atomic():
        categories = {c.nom.lower(): c for c in Categorie.objects.all()}
        for l in lignes:
            if l.categorie.lower() not in categories:
                categories[l.categorie.lower()] = Categorie.objects.create(nom=l.categorie)
                resultat.categories.append(l.categorie)
        if creer_emplacements and analyse.manquants:
            resultat.blocs, etageres = _creer_emplacements(analyse.manquants)
            resultat.etageres = [str(e) for e in etageres]
        etageres = {(e.bloc.code.lower(), e.code.lower()): e for e in Etagere.objects.select_related('bloc')}
        articles = []
        for l in lignes:
            etagere = etageres.get((l.bloc.lower(), l.etagere.lower())) if l.etagere else None
            if l.etagere and not etagere:
                resultat.non_ranges += 1
            articles.append(Article(
                code=l.code, designation=l.designation, categorie=categories[l.categorie.lower()], unite=l.unite,
                etagere=etagere, niveau=l.niveau if etagere else None, case=l.case if etagere else '',
                reference_fabricant=l.reference_fabricant, mots_cles=l.mots_cles, stock_min=l.stock_min,
                # Prix sans stock : il donne tout de suite la valeur des prochaines entrées sans prix.
                prix_moyen=l.prix if l.prix and not l.stock else 0))
        Article.objects.bulk_create(articles, batch_size=500)
        if any(a.pk is None for a in articles):
            pks = dict(Article.objects.filter(code__in=[a.code for a in articles]).values_list('code', 'pk'))
            for a in articles:
                a.pk = pks[a.code]
        a_entrer = [(a, l) for a, l in zip(articles, lignes) if l.stock]
        if a_entrer:
            bon = BonEntree.objects.create(
                cree_par=utilisateur, observation=f'Stock de départ (import Excel du {timezone.localdate():%d/%m/%Y})')
            LigneEntree.objects.bulk_create([
                LigneEntree(bon=bon, article=a, quantite=l.stock, prix_unitaire=l.prix or 0) for a, l in a_entrer])
            resultat.bon = services.valider(bon, utilisateur)
            resultat.nb_lignes_bon = len(a_entrer)
            resultat.valeur_stock = sum((l.valeur for _, l in a_entrer), Decimal('0'))
    resultat.duree = time.perf_counter() - debut
    return resultat


# =========================================================
# Fichier envoyé (gardé entre l'aperçu et l'import) et rapport
# =========================================================

def _dossier():
    dossier = Path(settings.DATA_DIR) / 'imports'
    dossier.mkdir(parents=True, exist_ok=True)
    return dossier


def _chemin(jeton, suffixe):
    if not re.fullmatch(r'[0-9a-f]{32}', str(jeton or '')):
        return None
    return _dossier() / f'{jeton}{suffixe}'


def garder(fichier):
    """Enregistre le fichier envoyé pour l'aperçu puis l'import ; renvoie son jeton. Les anciens sont effacés."""
    if fichier.size > MAX_OCTETS:
        raise ImportImpossible(f'Fichier trop gros ({fichier.size // (1024 * 1024)} Mo) : 10 Mo au plus.')
    limite = time.time() - 2 * 24 * 3600
    for ancien in _dossier().iterdir():
        try:
            if ancien.stat().st_mtime < limite:
                ancien.unlink()
        except OSError:
            pass
    jeton = secrets.token_hex(16)
    with open(_chemin(jeton, '.fichier'), 'wb') as sortie:
        for morceau in fichier.chunks():
            sortie.write(morceau)
    return jeton


def fichier_garde(jeton):
    chemin = _chemin(jeton, '.fichier')
    return chemin if chemin and chemin.is_file() else None


def oublier(jeton):
    chemin = fichier_garde(jeton)
    if chemin:
        chemin.unlink(missing_ok=True)


RESULTATS = {OK: 'Créé', AVERTISSEMENT: 'Créé (à vérifier)', ERREUR: 'Ignoré'}
RESULTATS_APERCU = {OK: 'Prêt', AVERTISSEMENT: 'Prêt (à vérifier)', ERREUR: 'Erreur : sera ignoré'}


def lignes_rapport(analyse, apres_import=False):
    """Lignes du rapport (JSON possible) : une par ligne du fichier, avec ce qui a été fait et pourquoi."""
    libelles = RESULTATS if apres_import else RESULTATS_APERCU
    return [[l.numero, libelles[l.statut], l.code + (' (automatique)' if l.code_auto else ''), l.designation,
             l.categorie if l.statut != ERREUR else '', l.unite if l.statut != ERREUR else '',
             str(l.stock) if l.stock is not None else '', str(l.prix) if l.prix is not None else '',
             l.emplacement, ' '.join(l.messages)] for l in analyse.lignes]


def garder_rapport(jeton, analyse, resultat):
    _chemin(jeton, '-rapport.json').write_text(json.dumps({
        'fichier': analyse.nom_fichier, 'date': timezone.localtime().strftime('%d/%m/%Y %H:%M'),
        'crees': resultat.crees, 'ignores': resultat.ignores, 'lignes': lignes_rapport(analyse, apres_import=True),
    }), encoding='utf-8')


def lire_rapport(jeton):
    chemin = _chemin(jeton, '-rapport.json')
    if not chemin or not chemin.is_file():
        return None
    return json.loads(chemin.read_text(encoding='utf-8'))


def rapport_excel(lignes, nom_fichier, titre, filtres, utilisateur=None):
    from .exports import PRIX, QTE, reponse_excel

    def nombre(texte):
        return Decimal(texte) if texte else ''

    return reponse_excel(
        titre, [('Ligne', QTE), 'Résultat', 'Code', 'Désignation', 'Catégorie', 'Unité', ('Stock de départ', QTE),
                ('Prix unitaire', PRIX), 'Emplacement', 'Remarques'],
        [l[:6] + [nombre(l[6]), nombre(l[7])] + l[8:] for l in lignes], nom_fichier, filtres=filtres,
        utilisateur=utilisateur)


# =========================================================
# Modèle à remplir
# =========================================================

def modele():
    """Classeur à remplir : feuille « Articles » (titres, liste des unités) et feuille « Aide »."""
    from openpyxl import Workbook
    from openpyxl.comments import Comment
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    from config import produit

    bleu, vert, gris = (PatternFill('solid', fgColor=c) for c in ('1F497D', '138A3C', 'EEF1F5'))
    blanc_gras = Font(bold=True, color='FFFFFF')
    largeurs = {'code': 16, 'designation': 42, 'categorie': 18, 'unite': 10, 'stock': 16, 'prix': 15,
                'emplacement': 16, 'stock_min': 16, 'mots_cles': 34, 'reference_fabricant': 22}
    classeur = Workbook()
    feuille = classeur.active
    feuille.title = 'Articles'
    for j, c in enumerate(COLONNES_MODELE, start=1):
        cellule = feuille.cell(1, j, c.titre + (' *' if c.obligatoire else ''))
        cellule.font, cellule.fill = blanc_gras, vert if c.obligatoire else bleu
        cellule.alignment = Alignment(vertical='center')
        cellule.comment = Comment(c.aide, produit.NOM, width=260, height=110)
        feuille.column_dimensions[get_column_letter(j)].width = largeurs.get(c.cle, 16)
    feuille.row_dimensions[1].height = 26
    feuille.freeze_panes = 'A2'
    colonne_unite = get_column_letter(next(j for j, c in enumerate(COLONNES_MODELE, start=1) if c.cle == 'unite'))
    liste = DataValidation(type='list', formula1='"' + ','.join(code for code, _ in Article.UNITES) + '"',
                           allow_blank=True, showErrorMessage=False)
    liste.add(f'{colonne_unite}2:{colonne_unite}{MAX_LIGNES + 1}')
    feuille.add_data_validation(liste)

    aide = classeur.create_sheet('Aide')
    aide.column_dimensions['A'].width = 22
    aide.column_dimensions['B'].width = 70
    aide.column_dimensions['C'].width = 26
    ligne = 1

    def ecrire(*valeurs, gras=False, titre=False, fond=None):
        nonlocal ligne
        for j, v in enumerate(valeurs, start=1):
            cellule = aide.cell(ligne, j, v)
            cellule.alignment = Alignment(wrap_text=True, vertical='top')
            if titre:
                cellule.font = Font(bold=True, size=14, color='1F497D')
            elif gras:
                cellule.font = Font(bold=True)
            if fond:
                cellule.fill = fond
                cellule.font = blanc_gras
        ligne += 1

    ecrire('Importer vos articles', titre=True)
    for texte in ('1. Remplissez la feuille « Articles » : une ligne par article, à partir de la ligne 2. Seule la '
                  'Désignation est obligatoire.',
                  '2. Enregistrez le fichier (Excel .xlsx ; le format CSV est accepté aussi).',
                  f'3. Dans {produit.NOM} : Articles › Importer depuis Excel, choisissez le fichier. Un aperçu montre '
                  "chaque ligne avant l'import : rien n'est enregistré avant que vous confirmiez.",
                  'Vous pouvez aussi envoyer votre propre fichier : les titres comme Réf, Libellé, Famille, Qté, PU ou '
                  'Seuil sont reconnus.'):
        aide.merge_cells(start_row=ligne, start_column=1, end_row=ligne, end_column=3)
        ecrire(texte)
    ligne += 1
    ecrire('Colonne', 'Ce qu’il faut mettre', 'Exemple', fond=bleu)
    for c in COLONNES:
        ecrire(c.titre + (' *' if c.obligatoire else ''), c.aide, c.exemple, gras=c.obligatoire)
    ligne += 1
    ecrire('Unité', 'Nom (autres écritures acceptées)', '', fond=bleu)
    for code, nom, autres in unites_acceptees():
        ecrire(code, nom + (f' ({autres})' if autres else ''), '')
    etageres = list(Etagere.objects.select_related('bloc').order_by('bloc__code', 'code'))
    ligne += 1
    ecrire('Emplacements du plan', 'Étagères (nombre de niveaux)', '', fond=bleu)
    if etageres:
        par_bloc = {}
        for e in etageres:
            par_bloc.setdefault(e.bloc, []).append(e)
        for bloc, liste_etageres in par_bloc.items():
            liste_etageres.sort(key=lambda e: _tri(e.code))
            ecrire(str(bloc), ', '.join(f'{e.code} ({e.nb_niveaux})' for e in liste_etageres), '')
    else:
        ecrire('', "Le plan du magasin est encore vide : les blocs et étagères cités dans le fichier pourront être "
                   "créés pendant l'import (vous les placerez ensuite dans Plan du magasin).", '')
    ligne += 1
    ecrire('Exemple de remplissage', gras=True)
    for j, c in enumerate(COLONNES_MODELE, start=1):
        cellule = aide.cell(ligne, j, c.titre)
        cellule.font, cellule.fill = blanc_gras, bleu
    ligne += 1
    exemples = [
        ('FER-HA10', 'Fer à béton HA 10 (barre 12 m)', 'Matériaux', 'BARRE', 300, 4200, 'D-M1-2', 50, 'fer de 10', ''),
        ('', 'Filtre à gasoil Volvo FMX', 'Filtres', 'U', 3, 21000, 'A-R1-3', 4, 'filtre gasoil', '21707134'),
        ('', 'Gants de manutention', 'EPI', 'paire', 15, 1500, '', 20, 'gants', ''),
    ]
    for exemple in exemples:
        for j, v in enumerate(exemple, start=1):
            aide.cell(ligne, j, v if v != '' else None).fill = gris
        ligne += 1

    reponse = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    reponse['Content-Disposition'] = 'attachment; filename="modele_import_articles.xlsx"'
    classeur.save(reponse)
    return reponse
