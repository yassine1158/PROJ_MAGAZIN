"""Plan du magasin en JSON : lecture (vue 3D, éditeur) et enregistrement depuis l'éditeur graphique.

Dans l'éditeur, toutes les positions sont absolues (en mètres, x vers la droite, z vers le fond).
En base, une étagère est rangée par rapport à son bloc : la conversion se fait ici.
"""
import math
from decimal import Decimal

from django.db import transaction
from django.db.models import Count

from .models import Bloc, Element, Etagere, Mur

# Objets que l'on peut poser sur le plan. « mural » : se colle à un mur et y ouvre un passage dans la 3D.
# Tailles en mètres (largeur × profondeur × hauteur). « allege » : hauteur du bas de la fenêtre.
TYPES_ELEMENTS = {
    'porte': {'nom': 'Porte', 'largeur': 0.9, 'profondeur': 0.2, 'hauteur': 2.1, 'couleur': '#a16207', 'mural': True},
    'porte_entree': {'nom': "Porte d'entrée", 'largeur': 2.0, 'profondeur': 0.2, 'hauteur': 2.4, 'couleur': '#16a34a',
                     'mural': True},
    'portail': {'nom': 'Portail / rideau', 'largeur': 4.0, 'profondeur': 0.2, 'hauteur': 4.0, 'couleur': '#64748b',
                'mural': True},
    'fenetre': {'nom': 'Fenêtre', 'largeur': 1.2, 'profondeur': 0.2, 'hauteur': 1.2, 'couleur': '#38bdf8',
                'mural': True, 'allege': 1.0},
    'bureau': {'nom': 'Bureau', 'largeur': 4.0, 'profondeur': 3.0, 'hauteur': 2.6, 'couleur': '#f59e0b'},
    'sanitaires': {'nom': 'Sanitaires', 'largeur': 2.5, 'profondeur': 2.0, 'hauteur': 2.6, 'couleur': '#06b6d4'},
    'poteau': {'nom': 'Poteau', 'largeur': 0.4, 'profondeur': 0.4, 'hauteur': 4.0, 'couleur': '#57534e'},
    'quai': {'nom': 'Quai de chargement', 'largeur': 6.0, 'profondeur': 3.0, 'hauteur': 1.2, 'couleur': '#a16207'},
    'zone': {'nom': 'Zone au sol', 'largeur': 4.0, 'profondeur': 4.0, 'hauteur': 0, 'couleur': '#22c55e'},
    'escalier': {'nom': 'Escalier', 'largeur': 1.2, 'profondeur': 3.0, 'hauteur': 3.0, 'couleur': '#78716c'},
    'extincteur': {'nom': 'Extincteur', 'largeur': 0.4, 'profondeur': 0.4, 'hauteur': 0.9, 'couleur': '#dc2626'},
    'texte': {'nom': 'Texte', 'largeur': 3.0, 'profondeur': 1.0, 'hauteur': 0, 'couleur': '#16202a'},
}

CENTIEME = Decimal('0.01')


def plan_json():
    blocs = Bloc.objects.prefetch_related('etageres').order_by('code')
    articles = dict(Etagere.objects.annotate(n=Count('articles')).values_list('pk', 'n'))
    return {
        'blocs': [
            {
                'id': b.pk, 'code': b.code, 'nom': b.nom, 'couleur': b.couleur,
                'x': float(b.x), 'z': float(b.z), 'largeur': float(b.largeur), 'profondeur': float(b.profondeur),
                'etageres': [
                    {
                        'id': e.pk, 'code': e.code, 'x': float(e.x), 'z': float(e.z),
                        'largeur': float(e.largeur), 'profondeur': float(e.profondeur),
                        'hauteur': float(e.hauteur), 'niveaux': e.nb_niveaux, 'tournee': e.tournee,
                        'articles': articles.get(e.pk, 0),
                    }
                    for e in b.etageres.all()
                ],
            }
            for b in blocs
        ],
        'murs': [
            {'id': m.pk, 'x1': float(m.x1), 'z1': float(m.z1), 'x2': float(m.x2), 'z2': float(m.z2),
             'epaisseur': float(m.epaisseur), 'hauteur': float(m.hauteur), 'couleur': m.couleur}
            for m in Mur.objects.all()
        ],
        'elements': [
            {'id': e.pk, 'type': e.type, 'nom': e.nom, 'x': float(e.x), 'z': float(e.z), 'largeur': float(e.largeur),
             'profondeur': float(e.profondeur), 'hauteur': float(e.hauteur), 'rotation': float(e.rotation),
             'couleur': e.couleur}
            for e in Element.objects.all()
        ],
    }


class PlanInvalide(Exception):
    def __init__(self, erreurs):
        super().__init__('; '.join(erreurs))
        self.erreurs = erreurs


def _nombre(valeur, nom, erreurs, mini=None, maxi=10000):
    try:
        n = float(valeur)
    except (TypeError, ValueError):
        erreurs.append(f'{nom} : nombre invalide.')
        return Decimal('0')
    if not math.isfinite(n) or (mini is not None and n < mini) or abs(n) > maxi:
        erreurs.append(f'{nom} : valeur hors limites ({valeur}).')
        return Decimal('0')
    return Decimal(str(n)).quantize(CENTIEME)


def _couleur(valeur, defaut):
    valeur = str(valeur or '')
    return valeur.lower() if len(valeur) == 7 and valeur.startswith('#') and all(
        c in '0123456789abcdefABCDEF' for c in valeur[1:]) else defaut


def _texte(valeur, longueur):
    return str(valeur or '').strip()[:longueur]


@transaction.atomic
def enregistrer(donnees):
    """Remplace le plan par celui de l'éditeur. Ce qui n'est plus dans l'éditeur est supprimé.

    donnees = {
      'blocs':    [{'cle', 'id'?, 'code', 'nom', 'couleur', 'x', 'z', 'largeur', 'profondeur'}],
      'etageres': [{'id'?, 'bloc' (cle du bloc), 'code', 'x', 'z', 'largeur', 'profondeur', 'hauteur',
                    'niveaux', 'tournee'}],        ← positions absolues
      'murs':     [{'id'?, 'x1', 'z1', 'x2', 'z2', 'epaisseur', 'hauteur', 'couleur'}],
      'elements': [{'type', 'nom', 'x', 'z' (centre), 'largeur', 'profondeur', 'hauteur', 'rotation', 'couleur'}],
    }
    """
    erreurs = []
    blocs_in = donnees.get('blocs') or []
    etageres_in = donnees.get('etageres') or []
    murs_in = donnees.get('murs') or []
    elements_in = donnees.get('elements') or []

    # --- Contrôles -------------------------------------------------------
    codes_blocs = {}
    for b in blocs_in:
        code = _texte(b.get('code'), 10)
        if not code:
            erreurs.append('Un bloc n\'a pas de code.')
        elif code.lower() in codes_blocs:
            erreurs.append(f'Deux blocs ont le même code « {code} ».')
        codes_blocs[code.lower()] = b
    cles = {str(b.get('cle')) for b in blocs_in}
    codes_etageres = set()
    for e in etageres_in:
        code = _texte(e.get('code'), 20)
        bloc = str(e.get('bloc'))
        if bloc not in cles:
            erreurs.append(f'L\'étagère « {code} » n\'est dans aucun bloc.')
        if not code:
            erreurs.append('Une étagère n\'a pas de code.')
        elif (bloc, code.lower()) in codes_etageres:
            erreurs.append(f'Deux étagères ont le même code « {code} » dans le même bloc.')
        codes_etageres.add((bloc, code.lower()))
        try:
            niveaux = int(e.get('niveaux'))
        except (TypeError, ValueError):
            niveaux = 0
        if not 1 <= niveaux <= 30:
            erreurs.append(f'Étagère « {code} » : le nombre de niveaux doit être entre 1 et 30.')
    for el in elements_in:
        if el.get('type') not in TYPES_ELEMENTS:
            erreurs.append(f'Objet inconnu : « {el.get("type")} ».')
    if erreurs:
        raise PlanInvalide(erreurs)

    # --- Blocs -----------------------------------------------------------
    # Codes provisoires d'abord : on peut ainsi échanger des codes, ou réutiliser celui d'un bloc supprimé.
    existants = {b.pk: b for b in Bloc.objects.all()}
    for pk in existants:
        Bloc.objects.filter(pk=pk).update(code=f'~{pk}')
    gardes = {int(b['id']) for b in blocs_in if b.get('id') and int(b['id']) in existants}
    blocs = {}
    for b in blocs_in:
        bloc = existants[int(b['id'])] if b.get('id') and int(b['id']) in gardes else Bloc()
        bloc.code = _texte(b.get('code'), 10)
        bloc.nom = _texte(b.get('nom'), 100)
        bloc.couleur = _couleur(b.get('couleur'), '#3b82f6')
        nom = f'Bloc {bloc.code}'
        bloc.x = _nombre(b.get('x'), nom, erreurs)
        bloc.z = _nombre(b.get('z'), nom, erreurs)
        bloc.largeur = _nombre(b.get('largeur'), f'{nom}, largeur', erreurs, mini=0.2, maxi=1000)
        bloc.profondeur = _nombre(b.get('profondeur'), f'{nom}, profondeur', erreurs, mini=0.2, maxi=1000)
        bloc.save()
        blocs[str(b.get('cle'))] = bloc

    # --- Étagères (avant de supprimer les blocs : une étagère déplacée garde ses articles) ---
    existantes = {e.pk: e for e in Etagere.objects.all()}
    for pk in existantes:
        Etagere.objects.filter(pk=pk).update(code=f'~{pk}')
    gardees = {int(e['id']) for e in etageres_in if e.get('id') and int(e['id']) in existantes}
    for e in etageres_in:
        etagere = existantes[int(e['id'])] if e.get('id') and int(e['id']) in gardees else Etagere()
        bloc = blocs[str(e.get('bloc'))]
        etagere.bloc = bloc
        etagere.code = _texte(e.get('code'), 20)
        nom = f'Étagère {etagere.code}'
        etagere.x = _nombre(e.get('x'), nom, erreurs) - bloc.x  # position absolue → relative au bloc
        etagere.z = _nombre(e.get('z'), nom, erreurs) - bloc.z
        etagere.largeur = _nombre(e.get('largeur'), f'{nom}, largeur', erreurs, mini=0.1, maxi=100)
        etagere.profondeur = _nombre(e.get('profondeur'), f'{nom}, profondeur', erreurs, mini=0.1, maxi=100)
        etagere.hauteur = _nombre(e.get('hauteur'), f'{nom}, hauteur', erreurs, mini=0.2, maxi=30)
        etagere.nb_niveaux = int(e.get('niveaux'))
        etagere.tournee = bool(e.get('tournee'))
        etagere.save()
    # Ce qui a été retiré dans l'éditeur ; les articles concernés deviennent « non rangés ».
    Etagere.objects.exclude(pk__in=gardees).filter(pk__in=list(existantes)).delete()
    Bloc.objects.exclude(pk__in=gardes).filter(pk__in=list(existants)).delete()

    # --- Murs ------------------------------------------------------------
    existants_murs = {m.pk: m for m in Mur.objects.all()}
    gardes_murs = {int(m['id']) for m in murs_in if m.get('id') and int(m['id']) in existants_murs}
    Mur.objects.exclude(pk__in=gardes_murs).delete()
    for m in murs_in:
        mur = existants_murs.get(int(m['id'])) if m.get('id') and int(m['id']) in gardes_murs else Mur()
        for champ in ('x1', 'z1', 'x2', 'z2'):
            setattr(mur, champ, _nombre(m.get(champ), 'Mur', erreurs))
        mur.epaisseur = _nombre(m.get('epaisseur', 0.2), 'Mur, épaisseur', erreurs, mini=0.05, maxi=5)
        mur.hauteur = _nombre(m.get('hauteur', 3), 'Mur, hauteur', erreurs, mini=0.2, maxi=30)
        mur.couleur = _couleur(m.get('couleur'), '#d6d3d1')
        if (mur.x1, mur.z1) == (mur.x2, mur.z2):
            continue  # mur de longueur nulle : ignoré
        mur.save()

    # --- Objets (portes, bureaux, poteaux…) : remplacés en entier ---------
    Element.objects.all().delete()
    for el in elements_in:
        defaut = TYPES_ELEMENTS[el['type']]
        nom = defaut['nom']
        Element.objects.create(
            type=el['type'], nom=_texte(el.get('nom'), 60),
            x=_nombre(el.get('x'), nom, erreurs), z=_nombre(el.get('z'), nom, erreurs),
            largeur=_nombre(el.get('largeur', defaut['largeur']), f'{nom}, largeur', erreurs, mini=0.05, maxi=200),
            profondeur=_nombre(el.get('profondeur', defaut['profondeur']), f'{nom}, profondeur', erreurs, mini=0.05, maxi=200),
            hauteur=_nombre(el.get('hauteur', defaut['hauteur']), f'{nom}, hauteur', erreurs, mini=0, maxi=30),
            rotation=_nombre(el.get('rotation') or 0, f'{nom}, rotation', erreurs, maxi=100000) % 360,
            couleur=_couleur(el.get('couleur'), defaut['couleur']),
        )

    if erreurs:
        raise PlanInvalide(erreurs)  # annule tout (transaction)
    return plan_json()
