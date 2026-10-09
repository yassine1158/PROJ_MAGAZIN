"""Sauvegardes des données (base + photos), clé USB, restauration et remise à zéro.

Une sauvegarde est un .zip dans <dossier de données>/sauvegardes : db.sqlite3, le dossier media/ et info.json.
Les fonctions qui reçoivent `dossier` n'utilisent pas Django : la restauration se fait au démarrage,
avant que la base soit ouverte (bureau.py, config/wsgi.py).
"""
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from config import produit

DOSSIER = 'sauvegardes'
EN_ATTENTE = 'restauration-en-attente.zip'
REFUSEE = 'restauration-refusee.zip'
ETAT = 'sauvegarde.json'
ANCIEN = 'restauration-ancien'
EXTRAIT = 'restauration-extrait'
ELEMENTS = ['db.sqlite3', 'db.sqlite3-journal', 'db.sqlite3-wal', 'db.sqlite3-shm', 'media']

GARDER_DERNIERES = 10
GARDER_MOIS = 12
DELAI_AUTO = timedelta(hours=24)
ATTENTE_MAX = 30  # secondes d'attente au plus si la base est occupée par une autre opération

NOM_FICHIER = re.compile(r'^.+-(\d{4}-\d{2}-\d{2}-\d{6})-([a-z0-9-]+)\.zip$')
RAISONS = {
    'auto': 'Automatique',
    'manuelle': 'Faite à la main',
    'avant-mise-a-jour': 'Avant une mise à jour',
    'avant-restauration': 'Avant une restauration',
    'avant-vidage': 'Avant effacement des données',
    'verification': 'Vérification',
}
EXEMPLE_REFERENCE = 'BL-DEMO-001'  # bon d'entrée créé par la commande demo
MODELES_A_VIDER = ['MouvementStock', 'LigneEntree', 'LigneSortie', 'LigneInventaire', 'BonEntree', 'BonSortie',
                   'Inventaire', 'Article', 'Categorie', 'Fournisseur', 'Chantier', 'Engin', 'Etagere', 'Bloc',
                   'Mur', 'Element']

journal = logging.getLogger(__name__)
_verrou = threading.Lock()


class ErreurSauvegarde(Exception):
    """Message en français, à montrer tel quel."""


def _racine(dossier=None):
    if dossier is not None:
        return Path(dossier)
    from django.conf import settings
    return Path(settings.DATA_DIR)


def _base_et_photos(dossier=None):
    if dossier is not None:
        return Path(dossier) / 'db.sqlite3', Path(dossier) / 'media'
    from django.conf import settings
    from django.db import connection
    return connection.settings_dict['NAME'], Path(settings.MEDIA_ROOT)


def dossier_sauvegardes(dossier=None):
    return _racine(dossier) / DOSSIER


def _supprimer(chemin):
    chemin = Path(chemin)
    if chemin.is_dir() and not chemin.is_symlink():
        shutil.rmtree(chemin)
    elif chemin.exists() or chemin.is_symlink():
        chemin.unlink()


def taille_lisible(octets):
    if octets < 1_000_000:
        return f'{max(1, round(octets / 1000))} Ko'
    if octets < 1_000_000_000:
        return f'{octets / 1_000_000:.1f} Mo'.replace('.', ',')
    return f'{octets / 1_000_000_000:.1f} Go'.replace('.', ',')


# =========================================================
# État (dernière copie sur clé USB, résultat de la dernière restauration)
# =========================================================

def lire_etat(dossier=None):
    try:
        return json.loads((_racine(dossier) / ETAT).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _ecrire_etat(dossier=None, **valeurs):
    etat = lire_etat(dossier)
    etat.update(valeurs)
    etat = {cle: valeur for cle, valeur in etat.items() if valeur is not None}
    try:
        (_racine(dossier) / ETAT).write_text(json.dumps(etat, ensure_ascii=False, indent=2), encoding='utf-8')
    except OSError:
        journal.warning("État des sauvegardes non enregistré", exc_info=True)


def oublier_resultat_restauration(dossier=None):
    _ecrire_etat(dossier, restauration=None)


# =========================================================
# Sauvegarder
# =========================================================

def _copier_base(base, copie):
    """Copie cohérente avec l'API « backup » de SQLite, même pendant que le logiciel travaille."""
    nom = str(base)
    uri = nom.startswith('file:')
    if not uri and not Path(nom).is_file():
        raise ErreurSauvegarde('Aucune base de données à sauvegarder.')
    source = sqlite3.connect(nom, uri=uri, timeout=ATTENTE_MAX)
    try:
        destination = sqlite3.connect(str(copie))
        try:
            debut = time.monotonic()

            def patienter(statut, restantes, total):
                if statut in (5, 6) and time.monotonic() - debut > ATTENTE_MAX:  # SQLITE_BUSY, SQLITE_LOCKED
                    raise ErreurSauvegarde('La base de données est occupée : réessayez dans un instant.')

            source.backup(destination, progress=patienter)
        finally:
            destination.close()
    finally:
        source.close()


def _nettoyer_temporaires(dossier_sauv):
    """Restes d'une sauvegarde interrompue (logiciel fermé pendant la copie)."""
    limite = time.time() - 3600
    for chemin in list(dossier_sauv.glob('.en-cours-*')) + list(dossier_sauv.glob('*.partiel')):
        try:
            if chemin.stat().st_mtime < limite:
                chemin.unlink()
        except OSError:
            pass


def sauvegarder(raison='manuelle', dossier=None):
    """Enregistre une sauvegarde (.zip) et renvoie son chemin. Lève ErreurSauvegarde en cas d'échec."""
    raison = re.sub(r'[^a-z0-9-]+', '-', str(raison).lower()).strip('-')[:40] or 'manuelle'
    base, photos = _base_et_photos(dossier)
    dossier_sauv = dossier_sauvegardes(dossier)
    with _verrou:
        try:
            dossier_sauv.mkdir(parents=True, exist_ok=True)
            _nettoyer_temporaires(dossier_sauv)
            maintenant = datetime.now().replace(microsecond=0)
            while True:
                nom = f'{produit.NOM}-{maintenant:%Y-%m-%d-%H%M%S}-{raison}.zip'
                if not (dossier_sauv / nom).exists():
                    break
                maintenant += timedelta(seconds=1)
            cible = dossier_sauv / nom
            copie = dossier_sauv / f'.en-cours-{os.getpid()}-{threading.get_ident()}.sqlite3'
            partiel = dossier_sauv / f'{nom}.partiel'
            try:
                copie.unlink(missing_ok=True)
                _copier_base(base, copie)
                with zipfile.ZipFile(partiel, 'w', zipfile.ZIP_DEFLATED, strict_timestamps=False) as archive:
                    archive.write(copie, 'db.sqlite3')
                    if photos.is_dir():
                        for fichier in sorted(photos.rglob('*')):
                            try:
                                if fichier.is_file():
                                    archive.write(fichier, 'media/' + fichier.relative_to(photos).as_posix())
                            except (OSError, ValueError):
                                journal.warning('Photo non sauvegardée : %s', fichier, exc_info=True)
                    archive.writestr('info.json', json.dumps({
                        'produit': produit.NOM, 'version': produit.VERSION,
                        'date': maintenant.isoformat(), 'raison': raison,
                    }, ensure_ascii=False, indent=2))
                os.replace(partiel, cible)
            finally:
                for temporaire in (copie, partiel):
                    try:
                        temporaire.unlink(missing_ok=True)
                    except OSError:
                        pass
        except ErreurSauvegarde:
            raise
        except (OSError, sqlite3.Error, zipfile.BadZipFile) as e:
            raise ErreurSauvegarde("La sauvegarde n'a pas pu être enregistrée (disque plein ou dossier protégé ?). "
                                   f'Détail : {e}') from e
        try:
            rotation(dossier_sauv)
        except OSError:
            journal.warning('Ménage des anciennes sauvegardes impossible', exc_info=True)
    return cible


def _liste(dossier_sauv):
    sauvegardes = []
    if not dossier_sauv.is_dir():
        return sauvegardes
    for chemin in dossier_sauv.glob('*.zip'):
        trouve = NOM_FICHIER.match(chemin.name)
        if not trouve:
            continue
        try:
            date = datetime.strptime(trouve.group(1), '%Y-%m-%d-%H%M%S')
            taille = chemin.stat().st_size
        except (ValueError, OSError):
            continue
        raison = trouve.group(2)
        sauvegardes.append({'nom': chemin.name, 'chemin': chemin, 'date': date, 'taille': taille,
                            'taille_lisible': taille_lisible(taille), 'raison': raison,
                            'libelle': RAISONS.get(raison, raison)})
    sauvegardes.sort(key=lambda s: (s['date'], s['nom']), reverse=True)
    return sauvegardes


def lister(dossier=None):
    """Sauvegardes, de la plus récente à la plus ancienne : nom, chemin, date, taille, raison, libelle."""
    return _liste(dossier_sauvegardes(dossier))


def trouver(nom, dossier=None):
    """Chemin d'une sauvegarde de la liste (jamais un autre fichier), ou None."""
    return next((s['chemin'] for s in lister(dossier) if s['nom'] == nom), None)


def rotation(dossier_sauv, maintenant=None):
    """Garde les 10 plus récentes, plus la plus récente de chaque mois sur 12 mois."""
    maintenant = maintenant or datetime.now()
    toutes = _liste(Path(dossier_sauv))
    garder = {s['nom'] for s in toutes[:GARDER_DERNIERES]}
    mois_gardes = set()
    actuel = maintenant.year * 12 + maintenant.month
    for s in toutes:
        mois = s['date'].year * 12 + s['date'].month
        if actuel - mois < GARDER_MOIS and mois not in mois_gardes:
            mois_gardes.add(mois)
            garder.add(s['nom'])
    for s in toutes:
        if s['nom'] not in garder:
            s['chemin'].unlink(missing_ok=True)


def sauvegarde_auto(dossier=None):
    """Une sauvegarde « auto » si aucune n'a été faite depuis 24 h ; renvoie son chemin ou None."""
    derniere = next(iter(lister(dossier)), None)
    if derniere and abs(datetime.now() - derniere['date']) < DELAI_AUTO:
        return None
    return sauvegarder('auto', dossier)


# =========================================================
# Clé USB (Windows)
# =========================================================

def cles_usb():
    """Lecteurs amovibles branchés : [{'racine': 'E:\\', 'nom': 'KINGSTON (E:)', 'libre': octets}]."""
    if sys.platform != 'win32':
        return []
    try:
        import ctypes

        noyau = ctypes.windll.kernel32
        ancien_mode = ctypes.c_uint()
        noyau.SetThreadErrorMode(1, ctypes.byref(ancien_mode))  # pas de fenêtre « insérez un disque »
        try:
            masque = noyau.GetLogicalDrives()
            cles = []
            for i in range(26):
                if not masque & (1 << i):
                    continue
                racine = f'{chr(65 + i)}:\\'
                if noyau.GetDriveTypeW(ctypes.c_wchar_p(racine)) != 2:  # DRIVE_REMOVABLE
                    continue
                try:
                    libre = shutil.disk_usage(racine).free
                except OSError:
                    continue  # lecteur de carte sans carte
                etiquette = ctypes.create_unicode_buffer(261)
                noyau.GetVolumeInformationW(ctypes.c_wchar_p(racine), etiquette, 261, None, None, None, None, 0)
                nom = f'{etiquette.value} ({racine[:2]})' if etiquette.value else f'Clé USB ({racine[:2]})'
                cles.append({'racine': racine, 'nom': nom, 'libre': libre})
            return cles
        finally:
            noyau.SetThreadErrorMode(ancien_mode.value, None)
    except Exception:
        journal.warning('Recherche des clés USB impossible', exc_info=True)
        return []


def copier_sur_cle(nom, racine=None, dossier=None):
    """Copie une sauvegarde dans <clé>\\<produit>\\ ; renvoie (chemin copié, clé). Lève ValueError."""
    source = trouver(nom, dossier)
    if not source:
        raise ValueError("Cette sauvegarde n'existe plus.")
    cles = cles_usb()
    if not cles:
        raise ValueError('Aucune clé USB trouvée. Branchez une clé USB, attendez quelques secondes puis réessayez.')
    cle = next((c for c in cles if c['racine'] == racine), None) if racine else cles[0]
    if not cle:
        raise ValueError("Cette clé USB n'est plus branchée. Choisissez une autre clé.")
    taille = source.stat().st_size
    if cle['libre'] < taille + 1_000_000:
        raise ValueError(f'La clé {cle["nom"]} est pleine : il faut au moins {taille_lisible(taille)} de libre.')
    cible = Path(cle['racine']) / produit.NOM / nom
    partiel = cible.with_name(nom + '.partiel')
    try:
        cible.parent.mkdir(exist_ok=True)
        shutil.copyfile(source, partiel)
        if partiel.stat().st_size != taille:
            raise OSError('copie incomplète')
        os.replace(partiel, cible)
    except OSError as e:
        try:
            partiel.unlink(missing_ok=True)
        except OSError:
            pass
        raise ValueError(f'La copie sur la clé {cle["nom"]} a échoué (clé retirée, pleine ou protégée en écriture). '
                         f'Détail : {e}') from e
    _ecrire_etat(dossier, copie_usb={'date': datetime.now().replace(microsecond=0).isoformat(),
                                     'cle': cle['nom'], 'nom': nom})
    return cible, cle


# =========================================================
# Restauration
# =========================================================

def _version(texte):
    try:
        return tuple(int(n) for n in str(texte).split('.'))
    except ValueError:
        return None


def _controler_base(base):
    try:
        connexion = sqlite3.connect(str(base))
        try:
            resultat = connexion.execute('PRAGMA integrity_check').fetchone()
            tables = {ligne[0] for ligne in connexion.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            connexion.close()
    except sqlite3.DatabaseError:
        raise ValueError('La base de données de cette sauvegarde est illisible (fichier abîmé).') from None
    if not resultat or resultat[0] != 'ok':
        raise ValueError('La base de données de cette sauvegarde est abîmée : elle ne peut pas être restaurée.')
    if 'django_migrations' not in tables or 'stock_article' not in tables:
        raise ValueError(f'Ce fichier ne contient pas les données de {produit.NOM}.')


def verifier_archive(chemin):
    """Contrôle qu'un .zip est une sauvegarde utilisable ; renvoie son info.json. Lève ValueError sinon."""
    try:
        with zipfile.ZipFile(chemin) as archive:
            noms = archive.namelist()
            for nom in noms:
                if nom.startswith(('/', '\\')) or ':' in nom or '..' in re.split(r'[\\/]', nom):
                    raise ValueError(f"Ce fichier n'est pas une sauvegarde de {produit.NOM} (chemins interdits).")
            if 'db.sqlite3' not in noms:
                raise ValueError(f"Ce fichier n'est pas une sauvegarde de {produit.NOM} "
                                 '(il ne contient pas de base de données).')
            abime = archive.testzip()
            if abime:
                raise ValueError(f'Ce fichier est abîmé ({abime}) : il ne peut pas être restauré.')
            try:
                info = json.loads(archive.read('info.json')) if 'info.json' in noms else {}
            except ValueError:
                info = {}
            if not isinstance(info, dict):
                info = {}
            version, actuelle = _version(info.get('version', '')), _version(produit.VERSION)
            if version and actuelle and version > actuelle:
                raise ValueError(f'Cette sauvegarde vient d\'une version plus récente ({info["version"]}). '
                                 f'Installez d\'abord cette version de {produit.NOM}.')
            with tempfile.TemporaryDirectory() as temporaire:
                base = Path(temporaire) / 'db.sqlite3'
                with archive.open('db.sqlite3') as source, open(base, 'wb') as copie:
                    shutil.copyfileobj(source, copie)
                _controler_base(base)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError):
        raise ValueError("Ce fichier n'est pas un fichier .zip valide (il est peut-être abîmé).") from None
    except OSError as e:
        raise ValueError(f'Ce fichier ne peut pas être lu : {e}') from None
    return info


def preparer_restauration(source, dossier=None):
    """Contrôle la sauvegarde (fichier envoyé ou chemin) et la met de côté pour le prochain démarrage.

    Rien n'est remplacé tout de suite : la base est utilisée par le logiciel.
    """
    racine = _racine(dossier)
    partiel = racine / (EN_ATTENTE + '.partiel')
    try:
        with open(partiel, 'wb') as copie:
            if isinstance(source, (str, Path)):
                with open(source, 'rb') as fichier:
                    shutil.copyfileobj(fichier, copie)
            else:
                for morceau in source.chunks():
                    copie.write(morceau)
        info = verifier_archive(partiel)
        os.replace(partiel, racine / EN_ATTENTE)
    except OSError as e:
        raise ValueError(f"Le fichier n'a pas pu être enregistré : {e}") from None
    finally:
        try:
            partiel.unlink(missing_ok=True)
        except OSError:
            pass
    return info


def restauration_en_attente(dossier=None):
    return (_racine(dossier) / EN_ATTENTE).exists()


def annuler_restauration(dossier=None):
    (_racine(dossier) / EN_ATTENTE).unlink(missing_ok=True)


def _remettre(racine, ancien):
    """Remet en place les fichiers mis de côté (retour à l'état d'avant)."""
    for nom in ELEMENTS:
        if (ancien / nom).exists():
            _supprimer(racine / nom)
            os.replace(ancien / nom, racine / nom)
    _supprimer(ancien)


def _reprendre_apres_coupure(racine):
    """Si l'ordinateur s'est éteint pendant un remplacement : on revient à l'état d'avant."""
    ancien = racine / ANCIEN
    if ancien.exists():
        if (racine / EN_ATTENTE).exists():
            _remettre(racine, ancien)
        else:
            _supprimer(ancien)  # remplacement terminé, seul le ménage manquait
    _supprimer(racine / EXTRAIT)


def _remplacer(racine, archive):
    extrait, ancien = racine / EXTRAIT, racine / ANCIEN
    _supprimer(extrait)
    extrait.mkdir()
    with zipfile.ZipFile(archive) as z:
        for nom in z.namelist():
            if (nom == 'db.sqlite3' or nom.startswith('media/')) and not nom.endswith('/'):
                z.extract(nom, extrait)
    (extrait / 'media').mkdir(exist_ok=True)
    (racine / 'media').mkdir(exist_ok=True)  # toujours déplacé : l'état d'avant reste reconnaissable
    ancien.mkdir()
    try:
        for nom in ELEMENTS:
            if (racine / nom).exists():
                os.replace(racine / nom, ancien / nom)
        os.replace(extrait / 'db.sqlite3', racine / 'db.sqlite3')
        os.replace(extrait / 'media', racine / 'media')
    except BaseException:
        _remettre(racine, ancien)
        _supprimer(extrait)
        raise
    archive.unlink()
    _supprimer(ancien)
    _supprimer(extrait)


def _resultat(racine, ok, message):
    resultat = {'ok': ok, 'message': message, 'date': datetime.now().replace(microsecond=0).isoformat()}
    _ecrire_etat(racine, restauration=resultat)
    (journal.info if ok else journal.error)(message)
    return resultat


def appliquer_restauration_en_attente(dossier=None):
    """Au démarrage, avant d'ouvrir la base : remet en place la sauvegarde choisie dans l'écran Sauvegarde.

    L'état actuel est d'abord sauvegardé ; en cas d'erreur, les données actuelles restent en place.
    Ne lève jamais d'exception ; renvoie {'ok', 'message', 'date'} (ou None s'il n'y avait rien à faire).
    """
    racine = _racine(dossier)
    try:
        _reprendre_apres_coupure(racine)
        attente = racine / EN_ATTENTE
        if not attente.exists():
            return None
        try:
            info = verifier_archive(attente)
        except ValueError as e:
            os.replace(attente, racine / REFUSEE)
            return _resultat(racine, False, f'Restauration refusée : {e}')
        base = racine / 'db.sqlite3'
        if base.is_file() and base.stat().st_size:
            sauvegarder('avant-restauration', racine)
        _remplacer(racine, attente)
        try:
            quand = datetime.fromisoformat(info.get('date', '')).strftime(' du %d/%m/%Y à %H:%M')
        except (TypeError, ValueError):
            quand = ''
        return _resultat(racine, True, f'Sauvegarde{quand} restaurée. Les données d\'avant la restauration '
                                       'ont été gardées dans une sauvegarde « Avant une restauration ».')
    except Exception as e:
        journal.exception('Restauration impossible')
        return _resultat(racine, False, f"La restauration n'a pas pu se faire ({e}). Vos données actuelles "
                                        "n'ont pas été modifiées. Elle sera retentée au prochain démarrage.")


# =========================================================
# Données d'exemple et remise à zéro
# =========================================================

def exemple_present():
    from .models import BonEntree
    return BonEntree.objects.filter(reference=EXEMPLE_REFERENCE).exists()


def compter_donnees():
    from .models import Article, Bloc, BonEntree, BonSortie, Categorie, Chantier, Engin, Etagere, Fournisseur, \
        Inventaire, MouvementStock
    return {
        'articles': Article.objects.count(),
        'bons': BonEntree.objects.count() + BonSortie.objects.count() + Inventaire.objects.count(),
        'mouvements': MouvementStock.objects.count(),
        'blocs': Bloc.objects.count(),
        'etageres': Etagere.objects.count(),
        'categories': Categorie.objects.count(),
        'fournisseurs': Fournisseur.objects.count(),
        'chantiers': Chantier.objects.count(),
        'engins': Engin.objects.count(),
    }


def _supprimer_photos(noms):
    from django.core.files.storage import default_storage
    for nom in noms:
        try:
            default_storage.delete(nom)
        except OSError:
            journal.warning('Photo non supprimée : %s', nom, exc_info=True)


def vider_donnees(sauvegarde_avant=True):
    """Efface articles, bons, mouvements, plan et listes ; garde la société, la licence et les comptes.

    Une sauvegarde est faite juste avant (si elle échoue, rien n'est effacé). Renvoie son chemin.
    """
    from django.apps import apps
    from django.db import connection, transaction

    chemin = sauvegarder('avant-vidage') if sauvegarde_avant else None
    modeles = [apps.get_model('stock', nom) for nom in MODELES_A_VIDER]
    with transaction.atomic():
        photos = list(apps.get_model('stock', 'Article').objects.exclude(photo='').values_list('photo', flat=True))
        for modele in modeles:
            modele.objects.all().delete()
        if connection.vendor == 'sqlite':
            tables = [modele._meta.db_table for modele in modeles]
            with connection.cursor() as curseur:
                curseur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'")
                if curseur.fetchone():
                    curseur.execute(f'DELETE FROM sqlite_sequence WHERE name IN ({", ".join(["%s"] * len(tables))})',
                                    tables)
        transaction.on_commit(lambda: _supprimer_photos(photos))
    return chemin
