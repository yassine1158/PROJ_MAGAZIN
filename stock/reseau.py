"""Accès depuis les téléphones du magasin (version bureau).

La fenêtre du logiciel utilise un serveur réservé à l'ordinateur (127.0.0.1). Quand l'accès est activé
(Réglages › Accès téléphones, fichier DATA_DIR/reseau.json), un second serveur écoute sur le Wi-Fi du magasin,
dans le même programme : les téléphones s'y connectent avec l'adresse http://<adresse de l'ordinateur>:<port>/.
"""
import base64
import ipaddress
import json
import logging
import socket
import sys
import threading

from django.conf import settings
from django.http.request import split_domain_port

FICHIER = 'reseau.json'
PORT_DEFAUT = 8765
ESSAIS_PORT = 6  # le port choisi, puis les 5 suivants s'il est occupé
RESEAUX_PRIVES = [ipaddress.ip_network(r) for r in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16')]
BOUCLE_LOCALE = ipaddress.ip_network('127.0.0.0/8')
NOMS_LOCAUX = {'localhost', '127.0.0.1', '::1'}

journal = logging.getLogger('stock')
_verrou = threading.Lock()
_etat = {}  # serveur, fil, port, erreur


class ErreurReseau(Exception):
    """Message en français, montré tel quel à l'utilisateur."""


# ---------- Réglage ----------

def lire_reglage():
    reglage = {'actif': False, 'port': PORT_DEFAUT}
    try:
        lu = json.loads((settings.DATA_DIR / FICHIER).read_text(encoding='utf-8'))
        reglage['actif'] = lu.get('actif') is True
        if port_valide(lu.get('port')):
            reglage['port'] = int(lu['port'])
    except (OSError, ValueError, AttributeError):
        pass
    return reglage


def ecrire_reglage(**valeurs):
    reglage = {**lire_reglage(), **valeurs}
    fichier = settings.DATA_DIR / FICHIER
    temporaire = fichier.with_suffix('.tmp')
    temporaire.write_text(json.dumps(reglage, indent=2), encoding='utf-8')
    temporaire.replace(fichier)
    return reglage


def port_valide(port):
    texte = str(port).strip()
    return texte.isascii() and texte.isdigit() and 1024 <= int(texte) <= 65535


# ---------- Adresses ----------

def _ipv4(texte):
    try:
        ip = ipaddress.ip_address(str(texte).strip('[]').split('%')[0])
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip if ip.version == 4 else None


def ip_privee(texte):
    ip = _ipv4(texte)
    return bool(ip) and any(ip in reseau for reseau in RESEAUX_PRIVES)


def ips_locales():
    """Adresses de cet ordinateur sur le réseau du magasin, la plus probable d'abord."""
    candidates = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('8.8.8.8', 80))  # aucun envoi : sert seulement à savoir quelle carte réseau est utilisée
            candidates.append(s.getsockname()[0])
    except OSError:
        pass  # pas d'accès internet : on regarde les cartes réseau
    try:
        candidates += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    ips = []
    for ip in candidates:
        if ip not in ips and not ip.startswith('127.') and ip_privee(ip):
            ips.append(ip)
    return sorted(ips, key=lambda ip: ip.startswith('169.254.'))  # adresse automatique de secours en dernier


def ip_locale():
    ips = ips_locales()
    return ips[0] if ips else None


def _url(ip, port):
    return f'http://{ip}:{port}/'


def port_actuel():
    return (est_actif() and _etat.get('port')) or lire_reglage()['port']


def adresse():
    """« http://192.168.1.20:8765/ » : l'adresse à taper sur le téléphone, ou None sans réseau."""
    ip = ip_locale()
    return _url(ip, port_actuel()) if ip else None


def autres_adresses():
    return [_url(ip, port_actuel()) for ip in ips_locales()[1:]]


def adresse_publique(request):
    """Adresse du logiciel pour un téléphone (QR codes) : l'adresse sur le Wi-Fi si l'accès est actif."""
    if est_actif():
        reseau = adresse()
        if reseau:
            return reseau
    return request.build_absolute_uri('/')


def est_local(request):
    """La demande vient de l'ordinateur lui-même (fenêtre du logiciel)."""
    ip = _ipv4(request.META.get('REMOTE_ADDR', ''))
    return request.META.get('REMOTE_ADDR') == '::1' or bool(ip and ip in BOUCLE_LOCALE)


def hote_autorise(hote):
    nom = split_domain_port(hote)[0].strip('[]')
    if not nom:
        return False
    if nom in NOMS_LOCAUX or ip_privee(nom):
        return True
    ip = _ipv4(nom)
    if ip and ip in BOUCLE_LOCALE:
        return True
    return nom in [h.lower() for h in settings.ALLOWED_HOSTS if h != '*']  # « testserver » pendant les tests


# ---------- Serveur pour les téléphones ----------

def est_actif():
    fil = _etat.get('fil')
    return bool(_etat.get('serveur') and fil and fil.is_alive())


def derniere_erreur():
    return _etat.get('erreur')


def _ouvrir_port(port):
    prise = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == 'win32':  # aucun autre programme ne peut alors prendre ce port
            try:
                prise.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except (AttributeError, OSError):
                pass
        else:
            prise.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        prise.bind(('0.0.0.0', port))
    except OSError:
        prise.close()
        raise
    return prise


def demarrer(application=None):
    """Ouvre l'accès des téléphones (port du réglage, ou l'un des 5 suivants s'il est pris). Renvoie le port."""
    from waitress import create_server

    if application is None:
        from django.core.wsgi import get_wsgi_application

        application = get_wsgi_application()
    voulu = lire_reglage()['port']
    with _verrou:
        if est_actif() and _etat['port_voulu'] == voulu:
            return _etat['port']
        _arreter()  # autre port demandé, ou serveur arrêté de lui-même
        for port in range(voulu, min(voulu + ESSAIS_PORT, 65536)):
            try:
                prise = _ouvrir_port(port)
                break
            except OSError:
                continue
        else:
            _etat['erreur'] = (f'Le port {voulu} et les suivants sont déjà utilisés par un autre programme. '
                               f'Choisissez un autre port (par exemple {9000 if voulu != 9000 else 8800}).')
            raise ErreurReseau(_etat['erreur'])
        try:
            serveur = create_server(application, sockets=[prise], threads=4, ident='')
        except Exception as e:
            prise.close()
            _etat['erreur'] = "L'accès des téléphones n'a pas pu démarrer."
            raise ErreurReseau(_etat['erreur']) from e
        fil = threading.Thread(target=serveur.run, name='acces-telephones', daemon=True)
        fil.start()
        _etat.update(serveur=serveur, fil=fil, port=port, port_voulu=voulu, erreur=None)
        return port


def demarrer_si_actif(application=None):
    """Au démarrage du logiciel : rouvre l'accès des téléphones s'il était activé. Ne lève jamais d'erreur."""
    if not lire_reglage()['actif']:
        return None
    try:
        return demarrer(application)
    except Exception as e:
        if not isinstance(e, ErreurReseau):
            _etat['erreur'] = "L'accès des téléphones n'a pas pu démarrer."
            journal.exception('Accès des téléphones impossible')
        return None


def _arreter():
    serveur, fil = _etat.pop('serveur', None), _etat.pop('fil', None)
    _etat.pop('port', None), _etat.pop('port_voulu', None)
    if not serveur:
        return
    from waitress import wasyncore

    try:  # tout est fermé par le fil du serveur lui-même (connexions ouvertes comprises)
        serveur.trigger.pull_trigger(lambda: wasyncore.close_all(serveur._map, ignore_all=True))
        if fil:
            fil.join(3)
    except Exception:
        try:
            serveur.close()
        except Exception:
            pass
    serveur.task_dispatcher.shutdown(timeout=3)


def arreter():
    with _verrou:
        _etat['erreur'] = None
        _arreter()


# ---------- QR code ----------

def qr_svg(texte):
    """QR code en SVG (dessiné par reportlab, sans autre bibliothèque)."""
    from reportlab.graphics import renderSVG
    from reportlab.graphics.barcode.qr import QrCodeWidget
    from reportlab.graphics.shapes import Drawing

    qr = QrCodeWidget(texte, barLevel='M', barBorder=4)
    qr.qr.make()
    cote = (qr.qr.getModuleCount() + 2 * qr.barBorder) * 4  # 4 unités par carré : traits nets
    qr.barWidth = qr.barHeight = cote
    dessin = Drawing(cote, cote)
    dessin.add(qr)
    svg = renderSVG.drawToString(dessin)
    return svg.replace('<svg ', '<svg shape-rendering="crispEdges" ', 1)


def qr_data_uri(texte):
    return 'data:image/svg+xml;base64,' + base64.b64encode(qr_svg(texte).encode('utf-8')).decode('ascii')


# ---------- Protection ----------

PAGE_REFUS = '''<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Accès refusé</title>
<style>body{{margin:0;font:17px/1.5 system-ui,"Segoe UI",sans-serif;background:#f3f5f7;color:#16202a}}
div{{max-width:520px;margin:12vh auto;padding:24px;background:#fff;border:1px solid #e3e7eb;border-radius:14px}}
h1{{font-size:21px;margin:0 0 10px}}</style></head><body><div><h1>{titre}</h1><p>{texte}</p></div></body></html>'''


def _refus(classe, titre, texte):
    return classe(PAGE_REFUS.format(titre=titre, texte=texte), content_type='text/html; charset=utf-8')


class ReseauMiddleware:
    """Version bureau : n'accepte que les adresses de l'ordinateur et du réseau du magasin.

    Refuse les noms de site inconnus (protection contre le « DNS rebinding »), les appareils hors du réseau du
    magasin, et tout autre appareil que l'ordinateur quand l'accès des téléphones est désactivé.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if settings.BUREAU:
            refus = self.controler(request)
            if refus:
                return refus
        return self.get_response(request)

    @staticmethod
    def controler(request):
        from django.http import HttpResponseBadRequest, HttpResponseForbidden

        hote = request.META.get('HTTP_HOST') or request.META.get('SERVER_NAME', '')
        if not hote_autorise(hote):
            return _refus(HttpResponseBadRequest, 'Adresse non reconnue',
                          'Tapez l’adresse affichée sur l’ordinateur du magasin, dans Réglages › Accès téléphones '
                          '(par exemple http://192.168.1.20:8765).')
        if est_local(request):
            return None
        if not ip_privee(request.META.get('REMOTE_ADDR', '')):
            return _refus(HttpResponseForbidden, 'Accès refusé',
                          'Seuls les téléphones connectés au Wi-Fi du magasin peuvent utiliser le logiciel.')
        if not est_actif():
            return _refus(HttpResponseForbidden, 'Accès des téléphones désactivé',
                          'Sur l’ordinateur du magasin, ouvrez Réglages › Accès téléphones et cliquez sur '
                          '« Activer l’accès des téléphones ».')
        if request.path.startswith('/bienvenue/'):
            return _refus(HttpResponseForbidden, 'Premier lancement',
                          'Le premier compte se crée sur l’ordinateur où le logiciel est installé.')
        return None


def media(request, path):
    """Photos servies par la version bureau : un téléphone doit être connecté (sauf pour le logo de la connexion)."""
    from django.http import Http404
    from django.views.static import serve

    if not (est_local(request) or request.user.is_authenticated or path.startswith('societe/')):
        raise Http404
    return serve(request, path, document_root=settings.MEDIA_ROOT)
