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


def livrer(request, reponse):
    """Dans la version bureau, un PDF ou un Excel est enregistré dans Documents puis ouvert directement.

    Ailleurs (navigateur, téléphone du magasin), la réponse est renvoyée telle quelle.
    """
    if not settings.BUREAU or not est_local(request):
        return reponse
    nom = re.search(r'filename="([^"]+)"', reponse.get('Content-Disposition', ''))
    chemin = dossier_documents() / (nom.group(1) if nom else 'document')
    chemin.write_bytes(reponse.content)
    try:
        ouvrir_fichier(chemin)
        messages.success(request, f'Fichier ouvert : {chemin}')
    except OSError:
        messages.warning(request, f'Fichier enregistré dans {chemin} (aucun programme pour l\'ouvrir).')
    retour = request.META.get('HTTP_REFERER') or '/'
    return redirect(retour)
