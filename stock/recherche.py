"""Recherche d'articles : « je cherche X » → l'article et son emplacement.

1. Recherche locale, immédiate : code (même tapé sans tiret ni espace), désignation, mots-clés,
   référence fabricant, catégorie et emplacement (« R1 », « bloc A »), avec tolérance aux pluriels
   et aux fautes (« ciman », « simen » → ciment).
2. Si une clé IA est configurée, l'IA comprend la demande (français courant ou ivoirien, abréviations,
   marques, dimensions, photo de la pièce) et la traduit en termes de recherche : ses résultats
   complètent ceux de l'étape 1.
Sans clé, ou si l'IA ne répond pas, l'étape 1 fonctionne seule.
"""
import base64
import io
import logging
import re
import threading
import time
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher

from django.conf import settings
from django.db.models import Count, Max
from django.db.models.signals import post_delete, post_save

from .models import Article, Bloc, Categorie, Etagere

logger = logging.getLogger(__name__)

TAILLE_MAX_PHOTO = 8 * 1024 * 1024
COTE_MAX_PHOTO = 1568
PAUSE_IA = 5 * 60  # après une panne de réseau, l'IA n'est plus appelée pendant 5 minutes
DUREE_INDEX = 120  # secondes ; l'index est aussi vidé à chaque enregistrement d'un article
SEUIL_FAUTE = 0.82

# Mots sans intérêt pour la recherche (« fer de 10 », « sac de ciment »).
MOTS_VIDES = {'de', 'du', 'des', 'la', 'le', 'les', 'un', 'une', 'en', 'et', 'au', 'aux', 'pour', 'avec', 'sur'}
# Mots qui annoncent un emplacement (« bloc A », « étagère R1 », « niveau 2 »).
MOTS_LIEU = {
    'bloc': 'bloc', 'blocs': 'bloc', 'zone': 'bloc',
    'etagere': 'etagere', 'etageres': 'etagere', 'etag': 'etagere', 'rayon': 'etagere', 'rayonnage': 'etagere',
    'niveau': 'niveau', 'niv': 'niveau',
}


class PhotoIllisible(ValueError):
    """La photo envoyée ne peut pas être lue : seule erreur de recherche montrée à l'utilisateur."""


# =========================================================
# Formes normalisées des mots
# =========================================================

_JETON = re.compile(r'[^\W_]+')
_CHIFFRE = re.compile(r'\d')
_ACCENTS = re.compile('[\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f\u064b-\u065f\u0670]')


def normaliser(texte):
    texte = str(texte or '').lower()
    if texte.isascii():
        return texte
    texte = texte.replace('œ', 'oe').replace('æ', 'ae')
    return _ACCENTS.sub('', unicodedata.normalize('NFKD', texte))


def _jetons(texte):
    return _JETON.findall(normaliser(texte))


def _mots(texte):
    return [m for m in _jetons(texte) if len(m) > 1]


def compacter(texte):
    """« FH VOL 01 », « fh-vol-01 » → « fhvol01 » : pour les codes tapés autrement."""
    return ''.join(_jetons(texte))


_SONS = [
    (r'ph', 'f'), (r'qu', 'k'), (r'ck', 'k'), (r'ch', 'sh'), (r'c(?=[eiy])', 's'), (r'[cq]', 'k'),
    (r'z', 's'), (r'y', 'i'), (r'(?<!s)h', ''), (r'eau|au', 'o'), (r'[ae]in', 'in'), (r'[ae]i', 'e'),
    (r'[ae][nm](?![aeiou])', 'an'), (r'om(?![aeiou])', 'on'), (r'gu(?=[ei])', 'g'),
]
_SONS = [(re.compile(motif), remplacement) for motif, remplacement in _SONS]
_DOUBLES = re.compile(r'(.)\1+')


def phonetique(mot):
    """Forme « qui se prononce pareil », pour les fautes : ciment, ciman, simen → siman ; gasoil, gazoil → gasoil.

    Volontairement légère (français courant) : seuls les mots de 4 lettres ou plus, sans chiffre, l'utilisent.
    """
    p = _DOUBLES.sub(r'\1', mot)
    for fin in ('sx', 'e', 'td'):  # pluriel, e muet, consonne finale muette
        if len(p) > 3 and p[-1] in fin:
            p = p[:-1]
    for motif, remplacement in _SONS:
        p = motif.sub(remplacement, p)
    return _DOUBLES.sub(r'\1', p)


# =========================================================
# Index en mémoire (formes normalisées de tous les articles actifs)
# =========================================================

class _Fiche:
    __slots__ = ('pk', 'code', 'code_norm', 'code_compact', 'ref_compact', 'categorie', 'bloc', 'etagere', 'niveau')


class _Index:
    """Mots de chaque article, déjà normalisés, avec leur poids (désignation 5, mots-clés 4, référence 6,
    catégorie 2). Une recherche ne fait plus que des recherches dans des dictionnaires ; la comparaison
    « faute de frappe » ne porte que sur le vocabulaire du catalogue, et une seule fois par mot cherché."""

    def __init__(self, lignes, empreinte):
        self.empreinte = empreinte
        self.cree = time.monotonic()
        self.fiches = []
        self.mots = defaultdict(dict)  # mot → {n° de fiche: poids}
        self.groupes = defaultdict(dict)  # « 15w » + « 40 » → « 15w40 » (dimensions, codes en morceaux)
        self.par_code = defaultdict(list)  # code compact → n° de fiche
        self._memo = {}
        for pk, code, designation, mots_cles, reference, categorie, etagere, bloc, niveau in lignes:
            i = len(self.fiches)
            f = _Fiche()
            f.pk, f.code, f.code_norm, f.code_compact = pk, code, normaliser(code), compacter(code)
            f.ref_compact, f.categorie = compacter(reference), normaliser(categorie)
            f.bloc, f.etagere, f.niveau = compacter(bloc), compacter(etagere), str(niveau or '')
            self.fiches.append(f)
            self.par_code[f.code_compact].append(i)
            for texte, poids in ((designation, 5), (mots_cles, 4), (reference, 6), (categorie, 2)):
                jetons = _jetons(texte)
                for m in jetons:
                    if len(m) > 1 and self.mots[m].get(i, 0) < poids:
                        self.mots[m][i] = poids
                chiffres = [bool(_CHIFFRE.search(j)) for j in jetons]
                for n in (2, 3):
                    for k in range(len(jetons) - n + 1):
                        if any(chiffres[k:k + n]):
                            groupe = ''.join(jetons[k:k + n])
                            if self.groupes[groupe].get(i, 0) < poids:
                                self.groupes[groupe][i] = poids
        self.par_longueur = defaultdict(list)
        self.sons = defaultdict(list)
        for m in self.mots:
            self.par_longueur[len(m)].append(m)
            if len(m) >= 4 and m.isalpha():
                self.sons[phonetique(m)].append(m)
        # Lus par plusieurs fils à la fois : plus de création de clé à la lecture.
        self.mots, self.groupes, self.par_code = dict(self.mots), dict(self.groupes), dict(self.par_code)
        self.par_longueur, self.sons = dict(self.par_longueur), dict(self.sons)
        self.lieux = {
            'bloc': {f.bloc for f in self.fiches if f.etagere},
            'etagere': {f.etagere for f in self.fiches if f.etagere},
            'bloc_etagere': {f.bloc + f.etagere for f in self.fiches if f.etagere},
        }

    def variantes(self, mot):
        """Mots du catalogue qui correspondent à « mot », avec un coefficient :
        1 identique, 0,7 même début (pluriel, mot coupé), 0,6 même son, 0,5 faute légère."""
        trouves = self._memo.get(mot)
        if trouves is not None:
            return trouves
        trouves = {mot: 1.0} if mot in self.mots else {}
        if len(mot) >= 4:
            for longueur, liste in self.par_longueur.items():
                if longueur < 4:
                    continue
                for m in liste:
                    if m != mot and (m.startswith(mot) or mot.startswith(m)):
                        trouves[m] = 0.7
            if mot.isalpha():
                son = phonetique(mot)
                if len(son) >= 3:
                    for m in self.sons.get(son, ()):
                        trouves.setdefault(m, 0.6)
            comparateur = SequenceMatcher(None, mot)
            for longueur in range(len(mot) - 3, len(mot) + 4):
                for m in self.par_longueur.get(longueur, ()):
                    if m in trouves:
                        continue
                    comparateur.set_seq2(m)
                    if (comparateur.real_quick_ratio() >= SEUIL_FAUTE and comparateur.quick_ratio() >= SEUIL_FAUTE
                            and comparateur.ratio() >= SEUIL_FAUTE):
                        trouves[m] = 0.5
        if len(self._memo) > 5000:
            self._memo.clear()
        self._memo[mot] = trouves
        return trouves

    def chercher(self, termes, categorie=None, limite=12):
        scores = defaultdict(float)
        for rang, terme in enumerate(termes):
            poids_terme = 1.0 if rang == 0 else 0.7
            t = normaliser(terme).strip()
            tc = compacter(t)
            if not tc:
                continue
            self._noter_code(scores, t, tc)
            mots = _mots(t)
            utiles = [m for m in mots if m not in MOTS_VIDES] or mots
            trouves = defaultdict(int)
            for mot in utiles:
                meilleur = {}
                for m, coef in self.variantes(mot).items():
                    for i, poids in self.mots[m].items():
                        if poids * coef > meilleur.get(i, 0):
                            meilleur[i] = poids * coef
                nombre_seul = 0.6 if mot.isdecimal() else 1  # « 10 » seul en dit peu : « fer de 10 » → d'abord le fer
                for i, valeur in meilleur.items():
                    scores[i] += valeur * poids_terme * nombre_seul
                    trouves[i] += 1
            if len(utiles) > 1:
                for i, n in trouves.items():
                    if n == len(utiles):
                        scores[i] += 6 * poids_terme  # tous les mots du terme sont présents
            # « 15 w 40 », « 15W-40 » → « 15W40 » ; « 1200R20 » → « 12.00 R20 » (dimensions, références)
            if len(tc) >= 3 and _CHIFFRE.search(tc):
                groupe = dict(self.groupes.get(tc, {}))
                if len(_jetons(t)) > 1:
                    for i, poids in self.mots.get(tc, {}).items():
                        groupe[i] = max(poids, groupe.get(i, 0))
                for i, poids in groupe.items():
                    scores[i] += (poids + 6) * poids_terme
            self._noter_lieu(scores, t, poids_terme)
        if categorie:
            categorie = normaliser(categorie)
            for i in scores:
                if scores[i] > 0 and self.fiches[i].categorie == categorie:
                    scores[i] += 3
        classes = sorted((i for i, s in scores.items() if s > 0), key=lambda i: (-scores[i], self.fiches[i].code))
        return [self.fiches[i].pk for i in classes[:limite]]

    def _noter_code(self, scores, t, tc):
        """Code article (exact, ou tapé sans tiret ni espace) et référence fabricant."""
        for i, f in enumerate(self.fiches):
            if t == f.code_norm or tc == f.code_compact:
                scores[i] += 50
            elif len(tc) >= 3 and (t in f.code_norm or tc in f.code_compact):
                scores[i] += 10
            if len(tc) >= 3 and tc == f.ref_compact:
                scores[i] += 30

    def _noter_lieu(self, scores, t, poids_terme):
        """« R1 », « A R1 », « A-R1 », « bloc A », « étagère R1 niveau 2 » : poids faible (après les noms)."""
        lectures = [lu for lu in _lire_lieu(t)
                    if all(valeur in self.lieux[cle] for cle, valeur in lu.items() if cle != 'niveau')]
        if not lectures:
            return
        for i, f in enumerate(self.fiches):
            if not f.etagere:
                continue
            meilleur = 0
            for lieu in lectures:
                if any(getattr(f, cle) != valeur for cle, valeur in lieu.items() if cle != 'bloc_etagere'):
                    continue
                if lieu.get('bloc_etagere', f.bloc + f.etagere) != f.bloc + f.etagere:
                    continue
                meilleur = max(meilleur, (2 if lieu.keys() <= {'bloc', 'niveau'} else 4) + ('niveau' in lieu))
            if meilleur:
                scores[i] += meilleur * poids_terme

    def article_par_code(self, texte):
        """L'article dont le code est exactement celui tapé (douchette, code recopié), s'il n'y en a qu'un."""
        fiches = self.par_code.get(compacter(texte), [])
        return self.fiches[fiches[0]].pk if len(fiches) == 1 else None


def _lire_lieu(t):
    """Lectures possibles d'un emplacement : liste de contraintes {'bloc', 'etagere', 'niveau', 'bloc_etagere'}."""
    jetons = _jetons(t)
    if not jetons or len(jetons) > 6:
        return []
    lieu, libres, k = {}, [], 0
    while k < len(jetons):
        cle = MOTS_LIEU.get(jetons[k])
        if cle and k + 1 < len(jetons):
            lieu[cle] = jetons[k + 1]
            k += 2
        else:
            libres.append(jetons[k])
            k += 1
    lectures = []
    if not libres:
        lectures.append(lieu)
    elif len(libres) == 1:
        libre = libres[0]
        if 'etagere' not in lieu:
            lectures.append({**lieu, 'etagere': libre})
            if 'bloc' not in lieu:
                lectures += [{**lieu, 'bloc': libre}, {**lieu, 'bloc_etagere': libre}]
        elif 'bloc' not in lieu:
            lectures.append({**lieu, 'bloc': libre})
    elif len(libres) == 2 and 'bloc' not in lieu and 'etagere' not in lieu:
        lectures.append({**lieu, 'bloc': libres[0], 'etagere': libres[1]})
    return [lu for lu in lectures if lu.keys() & {'bloc', 'etagere', 'bloc_etagere'}]


_verrou = threading.Lock()
_index = None
_generation = 0


def vider_index(sender=None, update_fields=None, **kwargs):
    """Appelé à chaque enregistrement d'un article (ou d'un bloc, d'une étagère, d'une catégorie)."""
    global _index, _generation
    if sender is Article and update_fields and set(update_fields) <= {'stock', 'prix_moyen'}:
        return  # simple mouvement de stock : les textes cherchés n'ont pas changé
    _generation += 1
    _index = None


for _modele in (Article, Bloc, Etagere, Categorie):
    post_save.connect(vider_index, sender=_modele, dispatch_uid=f'recherche-{_modele.__name__}-save')
    post_delete.connect(vider_index, sender=_modele, dispatch_uid=f'recherche-{_modele.__name__}-delete')


def _index_courant():
    global _index
    actifs = Article.objects.filter(actif=True)
    empreinte = tuple(actifs.aggregate(n=Count('pk'), m=Max('pk')).values())

    def valable(index):
        return index is not None and index.empreinte == empreinte and time.monotonic() - index.cree < DUREE_INDEX

    index = _index
    if valable(index):
        return index
    with _verrou:
        if valable(_index):
            return _index
        generation = _generation
        index = _Index(actifs.values_list('pk', 'code', 'designation', 'mots_cles', 'reference_fabricant',
                                          'categorie__nom', 'etagere__code', 'etagere__bloc__code', 'niveau'),
                       empreinte)
        if generation == _generation:
            _index = index
    return index


def _articles(pks):
    articles = Article.objects.filter(actif=True).select_related('categorie', 'etagere__bloc').in_bulk(pks)
    return [articles[pk] for pk in pks if pk in articles]


def recherche_locale(termes, categorie=None, limite=12):
    termes = [t for t in termes if t and str(t).strip()]
    if not termes:
        return []
    return _articles(_index_courant().chercher(termes, categorie, limite))


def article_par_code(texte):
    """Article dont le code correspond exactement au texte (tirets et espaces ignorés), sinon None."""
    if not compacter(texte):
        return None
    pk = _index_courant().article_par_code(texte)
    return next(iter(_articles([pk])), None) if pk else None


# =========================================================
# IA
# =========================================================

def _modele_interpretation():
    """Format de réponse demandé à l'IA (pydantic n'est chargé qu'au premier appel)."""
    global Interpretation
    if Interpretation is None:
        from pydantic import BaseModel, Field

        class _Interpretation(BaseModel):
            termes: list[str] = Field(description='Termes de recherche en français, du plus précis au plus général.')
            categorie: str | None = Field(default=None, description='Catégorie du catalogue la plus probable, ou null.')
            explication: str = Field(description="Une phrase courte, en français, disant ce que l'utilisateur cherche.")

        Interpretation = _Interpretation
    return Interpretation


Interpretation = None


CONSIGNES = """Tu aides le magasinier d'un magasin en Côte d'Ivoire (magasin de chantier, atelier mécanique,
pièces détachées, quincaillerie ou matériaux de construction) à retrouver un article dans son stock.
On te donne une demande (texte et/ou photo). Elle est écrite en français courant ou en français de Côte d'Ivoire,
souvent comme on parle, avec des abréviations (« fer de 10 » pour un fer à béton de 10 mm, « tôle bac »,
« gasoil »), des fautes d'orthographe, des noms de marques ou des dimensions.

Réponds avec :
- termes : 1 à 8 termes de recherche en français, du plus précis au plus général : le nom technique de
  l'article, ses synonymes courants, et les références, marques ou dimensions mentionnées
  (ex. « filtre gasoil volvo » → « filtre à gasoil », « filtre carburant », « Volvo » ;
  « fer de 10 » → « fer à béton 10 », « fer HA 10 », « fer à béton »). Pour une photo,
  identifie la pièce ou le matériau visible.
- categorie : la catégorie la plus probable parmi la liste fournie, ou null si aucune ne convient.
- explication : une phrase courte qui reformule ce qui est cherché.

Catégories du catalogue :
"""

_pause_ia_jusqu_a = 0.0


def ia_disponible():
    """IA configurée, et pas de panne de réseau dans les 5 dernières minutes."""
    return bool(settings.MAGASIN_IA_ACTIVE) and time.monotonic() >= _pause_ia_jusqu_a


def _image_bloc(photo):
    """Fichier envoyé → bloc image pour l'IA (réduit à 1568 px de côté)."""
    from PIL import Image, ImageOps

    if photo.size > TAILLE_MAX_PHOTO:
        raise PhotoIllisible('Photo trop lourde (8 Mo maximum).')
    try:
        image = ImageOps.exif_transpose(Image.open(photo))
        image.thumbnail((COTE_MAX_PHOTO, COTE_MAX_PHOTO))
        tampon = io.BytesIO()
        image.convert('RGB').save(tampon, format='JPEG', quality=85)
    except (OSError, ValueError, Image.DecompressionBombError):
        raise PhotoIllisible("Ce fichier n'est pas une photo lisible. Reprenez la photo.")
    return {
        'type': 'image',
        'source': {
            'type': 'base64',
            'media_type': 'image/jpeg',
            'data': base64.standard_b64encode(tampon.getvalue()).decode('ascii'),
        },
    }


def interpreter(question, photo=None):
    """Demande à l'IA ce que l'utilisateur cherche. Renvoie None si l'IA est indisponible."""
    global _pause_ia_jusqu_a
    if not ia_disponible():
        return None
    import anthropic

    categories = '\n'.join(f'- {c}' for c in Categorie.objects.values_list('nom', flat=True)) or '- (aucune)'
    contenu = []
    if photo is not None:
        contenu.append(_image_bloc(photo))
    contenu.append({'type': 'text', 'text': question or 'Quelle est cette pièce ou ce matériau ?'})

    try:
        reponse = anthropic.Anthropic(timeout=10, max_retries=0).beta.messages.parse(
            model=settings.MAGASIN_IA_MODELE,
            max_tokens=4000,
            system=CONSIGNES + categories,
            messages=[{'role': 'user', 'content': contenu}],
            output_format=_modele_interpretation(),
            output_config={'effort': 'low'},
            betas=['server-side-fallback-2026-07-01'],
            fallbacks='default',
        )
    except anthropic.AuthenticationError:
        logger.error('Recherche IA : clé ANTHROPIC_API_KEY invalide.')
        return None
    except anthropic.RateLimitError:
        logger.warning('Recherche IA : limite de requêtes atteinte.')
        return None
    except anthropic.APIStatusError as e:
        logger.warning('Recherche IA : erreur %s (%s).', e.status_code, e.message)
        return None
    except anthropic.APIConnectionError:  # pas d'Internet, ou délai dépassé
        logger.warning("Recherche IA : pas de réponse de l'API ; nouvel essai dans %s minutes.", PAUSE_IA // 60)
        _pause_ia_jusqu_a = time.monotonic() + PAUSE_IA
        return None

    if reponse.stop_reason != 'end_turn' or reponse.parsed_output is None:
        logger.warning('Recherche IA : réponse inutilisable (stop_reason=%s).', reponse.stop_reason)
        return None
    return reponse.parsed_output


def rechercher(question, photo=None, avec_ia=True, limite=12):
    """Renvoie (interprétation de l'IA ou None, articles trouvés).

    Seule une photo illisible lève une erreur (PhotoIllisible) ; toute autre panne de l'IA est journalisée
    et la recherche locale prend le relais.
    """
    question = (question or '').strip()
    interpretation = None
    if avec_ia or photo is not None:
        try:
            interpretation = interpreter(question, photo)
        except PhotoIllisible:
            raise
        except Exception:  # réponse invalide, coupée… : l'IA ne doit jamais bloquer la recherche
            logger.exception('Recherche IA : réponse inutilisable, recherche locale seule.')

    if interpretation:
        termes = list(interpretation.termes) + ([question] if question else [])
        resultats = recherche_locale(termes, interpretation.categorie, limite=limite)
        if not resultats and question:
            resultats = recherche_locale([question], limite=limite)
    else:
        resultats = recherche_locale([question], limite=limite)
    return interpretation, resultats
