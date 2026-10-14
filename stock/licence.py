"""Licence du logiciel : essai gratuit, puis activation par une clé liée à l'ordinateur.

Une clé = « données . signature », en base64. Les données : client|code machine|date.
Elle est signée (Ed25519) avec la clé privée du vendeur, qui ne quitte jamais son ordinateur
(outils/licences.py) ; le logiciel ne contient que la clé publique (config/produit.py).
Si aucune clé publique n'est configurée, le verrouillage est désactivé.
Le début de l'essai est gardé dans la base et dans des fichiers .essai (dossier de données, et sous Windows une
copie commune à tous les comptes dans ProgramData) : le plus ancien compte.
"""
import base64
import hashlib
import platform
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

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
    date_incorrecte: bool = False  # horloge de l'ordinateur reculée

    @property
    def bloque(self):
        return self.mode == 'expire'


def _fichier(nom):
    return settings.DATA_DIR / nom


def _aujourdhui():
    return date.today()


def _fichiers_essai():
    """Fichier .essai du dossier de données, et sa copie commune à tous les comptes Windows (ProgramData)."""
    fichiers = [_fichier('.essai')]
    commun = getattr(settings, 'ESSAI_COMMUN', None)
    if commun:
        fichiers.append(Path(commun))
    return fichiers


def _lire_essai(fichier):
    """(début, dernière date vue) d'un fichier .essai « début;dernière » ; l'ancien format n'a que le début."""
    try:
        parties = fichier.read_text().strip().split(';')
    except (OSError, ValueError):
        return None, None
    dates = []
    for texte in (parties + [''])[:2]:
        try:
            dates.append(date.fromisoformat(texte.strip()))
        except ValueError:
            dates.append(None)
    return dates[0], dates[1]


_ecriture_impossible = {}  # fichier → jour du dernier échec (un essai par jour suffit)


@dataclass
class Essai:
    debut: date
    jour: date              # jour de référence : jamais avant la dernière date vue
    date_incorrecte: bool


def _essai():
    """Début de l'essai (le plus ancien connu : base et fichiers) et jour de référence.

    Reculer l'horloge ne rend pas de jours d'essai : le jour de référence ne descend jamais sous la dernière
    date vue. Les fichiers sont réécrits seulement quand leur contenu change, donc au plus une fois par jour.
    """
    from .models import Parametres

    aujourdhui = _aujourdhui()
    p = Parametres.actuels()
    lus = {fichier: _lire_essai(fichier) for fichier in _fichiers_essai()}
    debuts = [d for d in [p.debut_essai] + [debut for debut, _ in lus.values()] if d]
    vues = [vue for _, vue in lus.values() if vue]
    debut = min(debuts) if debuts else aujourdhui
    vue = max(vues) if vues else None
    jour = max(aujourdhui, vue) if vue else aujourdhui
    if p.debut_essai != debut:
        p.debut_essai = debut
        p.save()
    for fichier, contenu in lus.items():
        if contenu == (debut, jour) or _ecriture_impossible.get(fichier) == jour:
            continue
        try:
            fichier.parent.mkdir(parents=True, exist_ok=True)
            fichier.write_text(f'{debut.isoformat()};{jour.isoformat()}')
        except OSError:  # copie commune créée par un autre compte Windows, disque protégé… : sans gravité
            _ecriture_impossible[fichier] = jour
    return Essai(debut, jour, bool(vue and (vue - aujourdhui).days > 1))


def debut_essai():
    """Date de début de l'essai : la plus ancienne connue, créée au premier appel."""
    return _essai().debut


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
    essai = _essai()
    restants = (essai.debut + timedelta(days=produit.ESSAI_JOURS) - essai.jour).days
    if restants > 0:
        return Etat('essai', jours_restants=min(restants, produit.ESSAI_JOURS), date_incorrecte=essai.date_incorrecte)
    return Etat('expire', date_incorrecte=essai.date_incorrecte)


def activer(texte):
    """Enregistre une clé valable ; lève ValueError sinon."""
    from .models import Parametres

    cle = lire_cle(texte)
    p = Parametres.actuels()
    p.cle_licence = ''.join(texte.split())
    p.save()
    return cle


CHEMINS_LIBRES = ('/activation/', '/static/', '/media/', '/connexion/', '/deconnexion/', '/bienvenue/', '/aide/')


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
