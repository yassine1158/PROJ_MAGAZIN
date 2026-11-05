"""Aides pour la version bureau (fenêtre Windows sans navigateur)."""
import os
import re
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect

from .reseau import est_local


def dossier_documents():
    """Documents\\<nom du produit> (créé si besoin)."""
    from config import produit

    documents = Path.home() / 'Documents'
    dossier = (documents if documents.is_dir() else settings.DATA_DIR) / produit.NOM
    dossier.mkdir(parents=True, exist_ok=True)
    return dossier


def ouvrir_fichier(chemin):
    if hasattr(os, 'startfile'):
        os.startfile(chemin)  # Windows : ouvre avec le programme habituel (lecteur PDF, Excel…)
    else:
        subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', str(chemin)])


def enregistrer(dossier, nom, contenu, essais=20, remplacer=True):
    """Écrit le fichier dans le dossier. S'il est déjà ouvert et verrouillé (Excel, lecteur PDF…),
    essaie « nom (2).ext », « nom (3).ext »… Renvoie le chemin écrit, ou None si tous sont verrouillés.

    remplacer=False : un fichier qui existe déjà n'est jamais remplacé, même fermé ; le nom suivant libre est pris.
    """
    base, extension = os.path.splitext(nom)
    for n in range(1, essais + 1):
        chemin = dossier / (nom if n == 1 else f'{base} ({n}){extension}')
        try:
            if remplacer:
                chemin.write_bytes(contenu)
            else:
                with chemin.open('xb') as sortie:  # création seulement
                    sortie.write(contenu)
            return chemin
        except FileExistsError:
            continue
        except PermissionError:
            if not chemin.exists():
                raise  # dossier protégé : ce n'est pas un fichier resté ouvert
    return None


def livrer(request, reponse, remplacer=True):
    """Dans la version bureau, un PDF ou un Excel est enregistré dans Documents puis ouvert directement.

    Ailleurs (navigateur, téléphone du magasin), la réponse est renvoyée telle quelle.
    remplacer=False : un fichier du même nom déjà dans Documents (modèle déjà rempli…) est gardé ; le nouveau
    prend le nom suivant, « nom (2).ext ».
    """
    if not settings.BUREAU or not est_local(request):
        return reponse
    retour = redirect(request.META.get('HTTP_REFERER') or '/')
    trouve = re.search(r'filename="([^"]+)"', reponse.get('Content-Disposition', ''))
    nom = Path(trouve.group(1)).name if trouve else ''
    nom = nom if nom.strip('.') else 'document'
    dossier = None
    try:
        dossier = dossier_documents()
        chemin = enregistrer(dossier, nom, reponse.content, remplacer=remplacer)
    except OSError:
        lieu = f' dans {dossier}' if dossier else ''
        messages.error(request, f"Le fichier {nom} n'a pas pu être enregistré{lieu}. Vérifiez que le disque n'est "
                                'pas plein et que Windows autorise ce dossier, puis recommencez.')
        return retour
    if chemin is None and not remplacer:
        messages.warning(request, f'Il y a déjà trop de fichiers {nom} dans {dossier} : rangez ou supprimez les '
                                  'anciens, puis recommencez.')
        return retour
    if chemin is None:
        messages.warning(request, f'Le fichier {nom} est déjà ouvert dans un autre programme (Excel, lecteur '
                                  f'PDF…). Fermez-le, puis recommencez. Il se trouve dans {dossier}.')
        return retour
    try:
        ouvrir_fichier(chemin)
        messages.success(request, f'Fichier ouvert : {chemin}')
    except OSError:
        messages.warning(request, f'Fichier enregistré dans {chemin} (aucun programme pour l\'ouvrir).')
    return retour
