"""Assistance : contacts du vendeur (config/produit.py) et rapport à envoyer quand quelque chose ne va pas."""
import io
import platform
import re
import sys
import zipfile
from datetime import datetime
from urllib.parse import quote

from config import produit

# Fichiers du dossier de données joints au rapport (s'ils existent) : jamais la base, les clés ni les photos.
FICHIERS_RAPPORT = ['journal.log', 'journal.log.1', 'erreur-demarrage.txt', 'verification.txt']


def numero_lisible(numero):
    """« 2250700000000 » → « +225 07 00 00 00 00 »."""
    chiffres = re.sub(r'\D', '', numero or '')
    if not chiffres:
        return ''
    if chiffres.startswith('225') and len(chiffres) == 13:
        reste = chiffres[3:]
        return '+225 ' + ' '.join(reste[i:i + 2] for i in range(0, 10, 2))
    return f'+{chiffres}'


def lien_whatsapp(texte=''):
    numero = re.sub(r'\D', '', produit.WHATSAPP or '')
    if not numero:
        return ''
    return f'https://wa.me/{numero}' + (f'?text={quote(texte)}' if texte else '')


def contacts():
    """Moyens de joindre le vendeur, seulement ceux qui sont renseignés."""
    liste = []
    if lien_whatsapp():
        liste.append({'nom': 'WhatsApp', 'texte': numero_lisible(produit.WHATSAPP), 'lien': lien_whatsapp()})
    telephone = (produit.TELEPHONE_VENDEUR or '').strip()
    if telephone:
        liste.append({'nom': 'Téléphone', 'texte': telephone, 'lien': 'tel:' + re.sub(r'[^\d+]', '', telephone)})
    email = (produit.EMAIL_VENDEUR or '').strip()
    if email:
        liste.append({'nom': 'E-mail', 'texte': email, 'lien': f'mailto:{email}'})
    return liste


def _windows():
    if sys.platform == 'win32':
        try:
            version, build, *_ = platform.win32_ver()
            return f'Windows {version} (build {build}) · {platform.machine()}'
        except Exception:
            pass
    return platform.platform()


def informations():
    """Texte lisible : version, système, et un aperçu des données (sans rien de confidentiel)."""
    from django.conf import settings

    from . import licence
    from .models import Article, BonEntree, BonSortie, Parametres

    lignes = [
        f'{produit.NOM} {produit.VERSION}',
        f'Rapport préparé le {datetime.now():%d/%m/%Y à %H:%M}',
        f'Système : {_windows()}',
        f'Python : {sys.version.split()[0]}',
        f'Version bureau : {"oui" if settings.BUREAU else "non"}',
        f'Dossier des données : {settings.DATA_DIR}',
    ]
    try:
        p = Parametres.actuels()
        etat = licence.etat()
        lignes += [
            f'Société : {p.nom_societe or "-"}',
            f'Articles : {Article.objects.count()}',
            f'Bons : {BonEntree.objects.count()} entrées, {BonSortie.objects.count()} sorties',
            f'Licence : {etat.mode}' + (f' ({etat.jours_restants} jours restants)' if etat.mode == 'essai' else ''),
            f'Code de l\'ordinateur : {licence.code_machine()}',
        ]
    except Exception as e:  # base illisible : le rapport sert justement dans ce cas
        lignes.append(f'Données illisibles : {e!r}')
    try:
        from .sauvegarde import lister

        sauvegardes = lister()
        lignes.append(f'Sauvegardes : {len(sauvegardes)}'
                      + (f', la dernière le {sauvegardes[0]["date"]:%d/%m/%Y à %H:%M}' if sauvegardes else ''))
    except Exception:
        pass
    return '\n'.join(lignes) + '\n'


def rapport():
    """Archive .zip pour l'assistance : informations.txt et les journaux du dossier de données.

    Renvoie (nom du fichier, contenu).
    """
    from django.conf import settings

    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('informations.txt', informations())
        for nom in FICHIERS_RAPPORT:
            chemin = settings.DATA_DIR / nom
            try:
                if chemin.is_file():
                    archive.writestr(nom, chemin.read_bytes())
            except OSError:
                pass
    return f'rapport-assistance-{datetime.now():%Y-%m-%d-%H%M}.zip', tampon.getvalue()
