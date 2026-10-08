"""Licence du logiciel : essai gratuit, puis activation par une clé liée à l'ordinateur.

Une clé = « données . signature », en base64. Les données : client|code machine|date.
Elle est signée (Ed25519) avec la clé privée du vendeur, qui ne quitte jamais son ordinateur
(outils/licences.py) ; le logiciel ne contient que la clé publique (config/produit.py).
Si aucune clé publique n'est configurée, le verrouillage est désactivé.
"""
import base64
import hashlib
import platform
import uuid
from dataclasses import dataclass
from datetime import date, timedelta

from django.conf import settings

from config import produit


def _b64(octets):
    return base64.urlsafe_b64encode(octets).decode().rstrip('=')


def _deb64(texte):
    return base64.urlsafe_b64decode(texte + '=' * (-len(texte) % 4))


def _identifiant_machine():
    """Identifiant stable de l'ordinateur (Windows : MachineGuid)."""
    if platform.system() == 'Windows':
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Cryptography',
                                0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as cle:
                return winreg.QueryValueEx(cle, 'MachineGuid')[0]
        except OSError:
            pass
    for chemin in ('/etc/machine-id', '/var/lib/dbus/machine-id'):
        try:
            with open(chemin) as f:
                return f.read().strip()
        except OSError:
            pass
    return str(uuid.getnode())


def code_machine():
    """Code court à communiquer au vendeur, ex. « 7F3A-91C2 »."""
    empreinte = hashlib.sha256(f'{produit.NOM}:{_identifiant_machine()}'.encode()).hexdigest().upper()
    return f'{empreinte[:4]}-{empreinte[4:8]}'


def signer(cle_privee, client, machine, jour=None):
    """Crée une clé d'activation (utilisé par l'outil du vendeur)."""
    client = client.replace('|', ' ').strip()
    donnees = f'{client}|{machine.strip().upper()}|{(jour or date.today()).isoformat()}'.encode()
    return f'{_b64(donnees)}.{_b64(cle_privee.sign(donnees))}'


@dataclass
class Cle:
    client: str
    machine: str
    jour: str


def lire_cle(texte, cle_publique=None):
    """Vérifie une clé. Renvoie Cle si elle est valable pour cet ordinateur, sinon lève ValueError."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    cle_publique = cle_publique if cle_publique is not None else produit.CLE_PUBLIQUE
    texte = ''.join((texte or '').split())  # espaces et retours à la ligne ignorés (copier-coller)
    try:
        donnees_b64, signature_b64 = texte.split('.')
        donnees, signature = _deb64(donnees_b64), _deb64(signature_b64)
        Ed25519PublicKey.from_public_bytes(_deb64(cle_publique)).verify(signature, donnees)
        client, machine, jour = donnees.decode().split('|')
    except (ValueError, InvalidSignature, UnicodeDecodeError):
        raise ValueError("Cette clé n'est pas valable. Vérifiez qu'elle est complète.")
    if machine not in ('*', code_machine()):
        raise ValueError(f'Cette clé a été faite pour un autre ordinateur ({machine}). '
                         f'Le code de cet ordinateur est {code_machine()}.')
    return Cle(client, machine, jour)


@dataclass
class Etat:
    mode: str               # 'libre' (pas de verrouillage), 'essai', 'active', 'expire'
    jours_restants: int = 0
    client: str = ''

    @property
    def bloque(self):
        return self.mode == 'expire'


def _fichier(nom):
    return settings.DATA_DIR / nom


def debut_essai():
    """Date de début de l'essai : la plus ancienne connue (base et fichier), créée au premier appel."""
    from .models import Parametres

    p = Parametres.actuels()
    dates = [p.debut_essai] if p.debut_essai else []
    try:
        dates.append(date.fromisoformat(_fichier('.essai').read_text().strip()))
    except (OSError, ValueError):
        pass
    debut = min(dates) if dates else date.today()
    if p.debut_essai != debut:
        p.debut_essai = debut
        p.save()
    try:
        fichier = _fichier('.essai')
        if not fichier.exists() or fichier.read_text().strip() != debut.isoformat():
            fichier.write_text(debut.isoformat())
    except OSError:
        pass
    return debut


def etat():
    if not produit.CLE_PUBLIQUE:
        return Etat('libre')
    from .models import Parametres

    cle = Parametres.actuels().cle_licence
    if cle:
        try:
            return Etat('active', client=lire_cle(cle).client)
        except ValueError:
            pass  # clé d'un autre ordinateur (base copiée) : retour à l'essai
    restants = (debut_essai() + timedelta(days=produit.ESSAI_JOURS) - date.today()).days
    return Etat('essai', jours_restants=max(0, restants)) if restants > 0 else Etat('expire')


def activer(texte):
    """Enregistre une clé valable ; lève ValueError sinon."""
    from .models import Parametres

    cle = lire_cle(texte)
    p = Parametres.actuels()
    p.cle_licence = ''.join(texte.split())
    p.save()
    return cle


CHEMINS_LIBRES = ('/activation/', '/static/', '/media/', '/connexion/', '/deconnexion/', '/bienvenue/')


class LicenceMiddleware:
    """Après l'essai, sans clé valable, toutes les pages mènent à l'écran d'activation (les données restent)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if produit.CLE_PUBLIQUE and not request.path.startswith(CHEMINS_LIBRES):
            request.licence = etat()
            if request.licence.bloque:
                from django.shortcuts import redirect
                return redirect('stock:activation')
        return self.get_response(request)
