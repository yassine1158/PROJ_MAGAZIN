"""Nouvelles versions du logiciel (version Windows).

Chaque version publiée est accompagnée d'un fichier version.json (créé par .github/workflows/windows.yml) :
{"version", "date", "taille_mo", "sha256", "url", "notes"}. Le logiciel le lit à l'adresse produit.MAJ_URL, en
arrière-plan peu après l'ouverture (bureau.py), au plus une fois par 24 h ; le résultat est gardé dans
<dossier de données>/mise-a-jour.json. Sans internet, rien n'est affiché : on réessaiera plus tard.
"""
import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from django.conf import settings

from config import produit

FICHIER = 'mise-a-jour.json'
INTERVALLE = timedelta(hours=24)       # entre deux vérifications réussies
INTERVALLE_ECHEC = timedelta(hours=1)  # après un échec (hors ligne…), pour ne pas réessayer à chaque ouverture
DELAI = 10                             # secondes
TAILLE_MAX = 64 * 1024

INJOIGNABLE = ("Impossible de joindre le serveur des mises à jour. Vérifiez que l'ordinateur est connecté à "
               "internet, puis réessayez.")
INVALIDE = 'Les informations de mise à jour reçues sont illisibles. Réessayez plus tard.'

journal = logging.getLogger('stock')
_verrou = threading.Lock()


class ErreurMiseAJour(Exception):
    """Message en français, montré tel quel après « Vérifier les mises à jour »."""


def numero(version):
    """« 1.10.0 » → (1, 10, 0, 0) ; None si ce n'est pas un numéro de version."""
    if not isinstance(version, str):
        return None
    trouve = re.fullmatch(r'v?(\d{1,6}(?:\.\d{1,6}){0,3})', version.strip(), re.ASCII)
    if not trouve:
        return None
    parties = [int(p) for p in trouve.group(1).split('.')]
    return tuple(parties + [0] * (4 - len(parties)))


def plus_recente(version, actuelle=None):
    """Vrai si `version` est plus récente que `actuelle` (par défaut la version installée) : 1.10.0 > 1.9.0."""
    nouvelle, installee = numero(version), numero(actuelle or produit.VERSION)
    return bool(nouvelle and installee and nouvelle > installee)


def _fichier():
    return settings.DATA_DIR / FICHIER


def lire_etat():
    try:
        etat = json.loads(_fichier().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return etat if isinstance(etat, dict) else {}


def _ecrire_etat(etat):
    fichier = _fichier()
    partiel = fichier.with_name(fichier.name + '.partiel')
    try:
        partiel.write_text(json.dumps(etat, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(partiel, fichier)
    except OSError:
        journal.warning('Résultat de la vérification des mises à jour non enregistré', exc_info=True)


def _date(texte):
    try:
        return datetime.fromisoformat(texte)
    except (TypeError, ValueError):
        return None


def _recente(texte, duree):
    date = _date(texte)
    return bool(date and timedelta(0) <= datetime.now() - date < duree)  # date future (horloge reculée) : non


def lire(info):
    """Contrôle le contenu de version.json ; renvoie les champs utiles ou lève ErreurMiseAJour."""
    if not isinstance(info, dict) or not numero(info.get('version')):
        raise ErreurMiseAJour(INVALIDE)
    url = info.get('url')
    adresse = urlsplit(url) if isinstance(url, str) else None
    if not adresse or adresse.scheme != 'https' or not adresse.netloc or re.search(r'[\s"<>]', url):
        raise ErreurMiseAJour(INVALIDE)
    notes = info.get('notes')
    taille = info.get('taille_mo')
    return {
        'version': info['version'].strip().lstrip('v'),
        'url': url,
        'notes': notes.replace('\r\n', '\n').strip()[:4000] if isinstance(notes, str) else '',
        'date': info['date'][:30] if isinstance(info.get('date'), str) else '',
        'taille_mo': taille if isinstance(taille, (int, float)) and not isinstance(taille, bool) else None,
        'sha256': info['sha256'][:64] if isinstance(info.get('sha256'), str) else '',
    }


def _telecharger():
    requete = urllib.request.Request(produit.MAJ_URL, headers={'User-Agent': f'{produit.NOM}/{produit.VERSION}'})
    try:
        with urllib.request.urlopen(requete, timeout=DELAI) as reponse:
            contenu = reponse.read(TAILLE_MAX + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404:  # aucune version publiée avec ce fichier pour l'instant
            raise ErreurMiseAJour("Aucune information de mise à jour n'est publiée pour l'instant. "
                                  'Réessayez plus tard.') from None
        raise ErreurMiseAJour(f'Le serveur des mises à jour ne répond pas correctement (erreur {e.code}). '
                              'Réessayez plus tard.') from None
    except Exception:  # hors ligne, délai dépassé, portail Wi-Fi, certificat…
        raise ErreurMiseAJour(INJOIGNABLE) from None
    if len(contenu) > TAILLE_MAX:
        raise ErreurMiseAJour(INVALIDE)
    try:
        info = json.loads(contenu.decode('utf-8-sig'))
    except ValueError:  # UnicodeDecodeError compris
        raise ErreurMiseAJour(INVALIDE) from None
    return lire(info)


def verifier_maintenant():
    """Lit version.json tout de suite. Renvoie la nouvelle version disponible (ou None) ; lève ErreurMiseAJour."""
    with _verrou:
        etat = lire_etat()
        maintenant = datetime.now().isoformat(timespec='seconds')
        try:
            info = _telecharger()
        except ErreurMiseAJour as e:
            etat.update(essai_le=maintenant, erreur=str(e))  # le résultat précédent est gardé
            _ecrire_etat(etat)
            raise
        _ecrire_etat({**info, 'verifie_le': maintenant})
    return derniere()


def verifier(force=False):
    """Vérification de fond : au plus une fois par 24 h (sauf `force`), sans jamais lever d'exception.

    Renvoie la nouvelle version disponible (dict : version, url, notes…) ou None.
    """
    try:
        etat = lire_etat()
        if not force and (_recente(etat.get('verifie_le'), INTERVALLE)
                          or _recente(etat.get('essai_le'), INTERVALLE_ECHEC)):
            return derniere()
        return verifier_maintenant()
    except ErreurMiseAJour as e:
        journal.info('Vérification des mises à jour impossible : %s', e)
    except Exception:
        journal.exception('Vérification des mises à jour impossible')
    return derniere()


def derniere():
    """Nouvelle version disponible d'après la dernière vérification (dict : version, url, notes…), ou None."""
    try:
        info = lire(lire_etat())
    except ErreurMiseAJour:
        return None
    return info if plus_recente(info['version']) else None


def resume():
    """Pour Réglages › Sauvegarde : nouvelle version disponible, dernière vérification réussie, échec éventuel."""
    etat = lire_etat()
    verifie_le, essai_le = _date(etat.get('verifie_le')), _date(etat.get('essai_le'))
    echec = bool(essai_le and (not verifie_le or essai_le > verifie_le) and isinstance(etat.get('erreur'), str))
    return {
        'disponible': derniere(),
        'verifie_le': verifie_le,
        'erreur': etat['erreur'] if echec else '',
        'essai_le': essai_le if echec else None,
    }
