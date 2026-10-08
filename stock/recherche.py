"""Recherche d'articles : « je cherche X » → l'article et son emplacement.

1. Si une clé IA est configurée, l'IA comprend la demande (français, darija,
   arabe, fautes d'orthographe, nom courant ou photo de la pièce) et la traduit
   en termes de recherche.
2. Les termes sont ensuite cherchés dans le catalogue local (code, désignation,
   mots-clés, référence fabricant, catégorie), avec tolérance aux fautes.
Sans clé ou si l'IA ne répond pas, l'étape 2 fonctionne seule.
"""
import base64
import io
import logging
import unicodedata
from difflib import SequenceMatcher

from django.conf import settings

from .models import Article, Categorie

logger = logging.getLogger(__name__)

TAILLE_MAX_PHOTO = 8 * 1024 * 1024
COTE_MAX_PHOTO = 1568


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


CONSIGNES = """Tu aides le magasinier d'une entreprise de BTP et de béton à retrouver un article dans son magasin.
On te donne une demande (texte et/ou photo). Elle peut être en français, en darija tunisienne ou ivoirienne
(en lettres latines ou arabes), en arabe, avec des fautes, des abréviations ou des noms de chantier.

Réponds avec :
- termes : 1 à 8 termes de recherche en français, du plus précis au plus général : le nom technique de
  l'article, ses synonymes courants, et les références, marques ou dimensions mentionnées
  (ex. « filtre zit volvo » → « filtre à huile », « filtre huile », « Volvo »). Pour une photo,
  identifie la pièce ou le matériau visible.
- categorie : la catégorie la plus probable parmi la liste fournie, ou null si aucune ne convient.
- explication : une phrase courte qui reformule ce qui est cherché.

Catégories du catalogue :
"""


def normaliser(texte):
    texte = unicodedata.normalize('NFKD', str(texte or '').lower())
    return ''.join(c for c in texte if not unicodedata.combining(c))


def _mots(texte):
    return [m for m in ''.join(c if c.isalnum() else ' ' for c in normaliser(texte)).split() if len(m) > 1]


def _proche(mot, mots, seuil=0.82):
    return any(SequenceMatcher(None, mot, m).ratio() >= seuil for m in mots if abs(len(m) - len(mot)) <= 3)


def _score(article, termes, categorie):
    code = normaliser(article.code)
    champs = {
        'designation': (_mots(article.designation), 5),
        'mots_cles': (_mots(article.mots_cles), 4),
        'reference': (_mots(article.reference_fabricant), 6),
        'categorie': (_mots(article.categorie.nom), 2),
    }
    score = 0
    for rang, terme in enumerate(termes):
        poids_terme = 1.0 if rang == 0 else 0.7
        t = normaliser(terme).strip()
        if not t:
            continue
        if t == code:
            score += 50
        elif len(t) >= 3 and t in code:
            score += 10
        mots_terme = _mots(t)
        if not mots_terme:
            continue
        trouves = 0
        for mot in mots_terme:
            meilleur = 0
            for mots_champ, poids in champs.values():
                if mot in mots_champ:
                    meilleur = max(meilleur, poids)
                elif len(mot) >= 4 and any(m.startswith(mot) or mot.startswith(m) for m in mots_champ if len(m) >= 4):
                    meilleur = max(meilleur, poids * 0.7)
                elif len(mot) >= 4 and _proche(mot, mots_champ):
                    meilleur = max(meilleur, poids * 0.5)
            if meilleur:
                trouves += 1
                score += meilleur * poids_terme
        if trouves == len(mots_terme) and len(mots_terme) > 1:
            score += 6 * poids_terme  # tous les mots du terme sont présents
    if score and categorie and normaliser(categorie) == normaliser(article.categorie.nom):
        score += 3
    return score


def recherche_locale(termes, categorie=None, limite=12):
    termes = [t for t in termes if t and t.strip()]
    if not termes:
        return []
    articles = Article.objects.filter(actif=True).select_related('categorie', 'etagere__bloc')
    scores = [(s, a) for a in articles if (s := _score(a, termes, categorie)) > 0]
    scores.sort(key=lambda sa: (-sa[0], sa[1].code))
    return [a for _, a in scores[:limite]]


def _image_bloc(photo):
    """Fichier envoyé → bloc image pour l'IA (réduit à 1568 px de côté)."""
    from PIL import Image, ImageOps

    if photo.size > TAILLE_MAX_PHOTO:
        raise ValueError('Photo trop lourde (8 Mo maximum).')
    try:
        image = ImageOps.exif_transpose(Image.open(photo))
    except (OSError, Image.DecompressionBombError):
        raise ValueError("Ce fichier n'est pas une photo lisible.")
    image.thumbnail((COTE_MAX_PHOTO, COTE_MAX_PHOTO))
    tampon = io.BytesIO()
    image.convert('RGB').save(tampon, format='JPEG', quality=85)
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
    if not settings.MAGASIN_IA_ACTIVE:
        return None
    import anthropic

    categories = '\n'.join(f'- {c}' for c in Categorie.objects.values_list('nom', flat=True)) or '- (aucune)'
    contenu = []
    if photo is not None:
        contenu.append(_image_bloc(photo))
    contenu.append({'type': 'text', 'text': question or 'Quelle est cette pièce ou ce matériau ?'})

    try:
        reponse = anthropic.Anthropic(timeout=60).beta.messages.parse(
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
    except anthropic.APIConnectionError:
        logger.warning('Recherche IA : pas de connexion à l\'API.')
        return None

    if reponse.stop_reason != 'end_turn' or reponse.parsed_output is None:
        logger.warning('Recherche IA : réponse inutilisable (stop_reason=%s).', reponse.stop_reason)
        return None
    return reponse.parsed_output


def rechercher(question, photo=None):
    question = (question or '').strip()
    interpretation = None
    try:
        interpretation = interpreter(question, photo)
    except ValueError:
        raise
    except Exception:  # l'IA ne doit jamais bloquer la recherche
        logger.exception('Recherche IA : erreur inattendue.')

    if interpretation:
        termes = interpretation.termes + ([question] if question else [])
        resultats = recherche_locale(termes, interpretation.categorie)
        if not resultats and question:
            resultats = recherche_locale([question])
    else:
        resultats = recherche_locale([question])
    return interpretation, resultats
