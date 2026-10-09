"""Version bureau du logiciel (Windows).

Ouvre le logiciel dans sa propre fenêtre (sans navigateur ni fenêtre noire). Un écran de chargement
s'affiche tout de suite, pendant que le logiciel démarre en arrière-plan.
Les données sont gardées dans %LOCALAPPDATA%\\<nom du produit> (base, photos, clés).

    python bureau.py           ouvre le logiciel
    python bureau.py --test    vérifie que tout démarre, sans ouvrir de fenêtre (utilisé à la construction)
"""
import html
import json
import os
import shutil
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

from config import produit

ANCIENS_DOSSIERS = ['Magasin SI BETON']  # versions précédentes : les données sont reprises
_verrou = None  # Windows : marque « logiciel déjà ouvert » (voir instance_unique)


class ErreurDemarrage(Exception):
    """Message en français, affiché tel quel sur l'écran de chargement."""


def dossier_donnees():
    base = Path(os.environ.get('LOCALAPPDATA') or os.path.join(Path.home(), '.local', 'share'))
    dossier = base / produit.NOM
    if not dossier.exists():
        for ancien in ANCIENS_DOSSIERS:
            if (base / ancien / 'db.sqlite3').exists():
                shutil.move(str(base / ancien), str(dossier))
                break
    dossier.mkdir(parents=True, exist_ok=True)
    return dossier


def preparer(dossier, afficher=lambda message: None):
    os.environ['MAGASIN_BUREAU'] = '1'
    os.environ['MAGASIN_DATA_DIR'] = str(dossier)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    from stock import sauvegarde

    if sauvegarde.restauration_en_attente(dossier):
        afficher('Restauration de la sauvegarde…')
    sauvegarde.appliquer_restauration_en_attente(dossier)  # avant d'ouvrir la base
    base = dossier / 'db.sqlite3'
    base_existante = base.is_file() and base.stat().st_size > 0
    import django

    django.setup()
    from django.core.management import call_command
    from django.core.management.commands.migrate import Command as Migrate
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    executeur = MigrationExecutor(connection)
    if executeur.migration_plan(executeur.loader.graph.leaf_nodes()):  # base à créer ou à mettre à jour
        if base_existante:
            afficher('Mise à jour des données…')
            try:
                sauvegarde.sauvegarder('avant-mise-a-jour')
            except sauvegarde.ErreurSauvegarde as e:
                raise ErreurDemarrage(f'Les données n\'ont pas pu être sauvegardées avant la mise à jour, '
                                      f'qui n\'a donc pas été faite.\n{e}\nLibérez de la place sur le disque '
                                      'puis rouvrez le logiciel.') from e
        call_command(Migrate(), interactive=False, verbosity=0)


def sauvegarde_de_fond(delai=30):
    """Une sauvegarde par jour, faite en arrière-plan peu après l'ouverture (sans ralentir le démarrage)."""
    time.sleep(delai)
    try:
        from stock import sauvegarde

        sauvegarde.sauvegarde_auto()
    except Exception:
        import logging

        logging.getLogger('stock').exception('Sauvegarde automatique impossible')


def mises_a_jour_de_fond(delai=20):
    """Nouvelle version publiée ? Vérifié peu après l'ouverture, au plus une fois par jour, sans rien dire hors ligne."""
    time.sleep(delai)
    try:
        from stock import mises_a_jour

        mises_a_jour.verifier()
    except Exception:
        import logging

        logging.getLogger('stock').exception('Vérification des mises à jour impossible')


def instance_unique():
    """Windows : une seule copie du logiciel ouverte à la fois (deux copies abîmeraient la même base).

    Si le logiciel est déjà ouvert, sa fenêtre est ramenée devant et on renvoie False.
    En cas d'erreur, le logiciel s'ouvre normalement.
    """
    global _verrou
    if sys.platform != 'win32':
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        _verrou = kernel32.CreateMutexW(None, False, produit.MUTEX)  # gardé ouvert jusqu'à la fermeture
        if ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
            return True
    except Exception:
        return True
    try:
        ramener_fenetre()
    except Exception:
        pass
    return False


def ramener_fenetre():
    """Met devant la fenêtre du logiciel déjà ouvert (et la rouvre si elle était réduite)."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL('user32')
    user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowExW.restype = wintypes.HWND
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    for nom in ('IsIconic', 'SetForegroundWindow'):
        getattr(user32, nom).argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    classe = ctypes.create_unicode_buffer(256)
    fenetre = None
    while True:
        fenetre = user32.FindWindowExW(None, fenetre, None, produit.NOM)  # comme FindWindowW, en continuant
        if not fenetre:
            return
        user32.GetClassNameW(fenetre, classe, len(classe))
        if classe.value != 'CabinetWClass':  # pas un dossier « MagaStock » ouvert dans l'Explorateur
            break
    if user32.IsIconic(fenetre):
        user32.ShowWindow(fenetre, 9)  # SW_RESTORE
    user32.SetForegroundWindow(fenetre)


def acces_telephones():
    """Réglages › Accès téléphones : rouvre l'accès par le Wi-Fi du magasin s'il était activé."""
    try:
        from config.wsgi import application
        from stock import reseau

        reseau.demarrer_si_actif(application)
    except Exception:
        import logging

        logging.getLogger('stock').exception('Accès des téléphones impossible')


def port_libre():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def demarrer_serveur(port):
    from waitress import create_server

    from config.wsgi import application

    serveur = create_server(application, host='127.0.0.1', port=port, threads=8)
    threading.Thread(target=serveur.run, daemon=True).start()
    for _ in range(100):
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                return serveur
        except OSError:
            time.sleep(0.1)
    raise RuntimeError('Le serveur interne ne démarre pas.')


def demarrer(dossier, afficher=lambda message: None):
    preparer(dossier, afficher)
    port = port_libre()
    return demarrer_serveur(port), f'http://127.0.0.1:{port}/'


def verifier(url, dossier):
    """Contrôle sans fenêtre (à la construction) : pages, fichiers, exemple, PDF, Excel, écrans, fenêtre."""
    lignes = []
    for chemin in ['bienvenue/', 'connexion/', 'static/stock/app.css', 'static/stock/bon.js',
                   'static/stock/vendor/inter/inter-latin-wght-normal.woff2', 'static/admin/css/base.css',
                   'static/stock/vendor/three/three.module.min.js', 'static/stock/editeur-plan.js', 'api/plan/', 'admin/login/']:
        with urllib.request.urlopen(url + chemin, timeout=20) as reponse:
            assert reponse.status == 200, chemin
            lignes.append(f'OK {chemin} ({len(reponse.read())} octets)')
    from django.core.management import call_command

    from stock import exports
    from stock.management.commands.demo import Command as Demo
    from stock.models import Article, BonSortie
    from stock.pdf import reponse_pdf

    if not Article.objects.exists():
        call_command(Demo(), stdout=open(os.devnull, 'w'))
    lignes.append(f'OK exemple ({Article.objects.count()} articles)')
    from stock import sauvegarde

    archive = sauvegarde.sauvegarder('verification')
    sauvegarde.verifier_archive(archive)
    lignes.append(f'OK sauvegarde ({archive.stat().st_size} octets, {len(sauvegarde.lister())} au total)')
    lignes.append(f'OK PDF ({len(reponse_pdf([BonSortie.objects.first()], "test").content)} octets)')
    lignes.append(f'OK Excel ({len(exports.etat_stock(Article.objects.all()).content)} octets)')
    # Écrans après connexion (vérifie que gabarits et balises sont bien inclus dans le programme).
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.test import Client

    settings.ALLOWED_HOSTS.append('testserver')
    testeur, _ = get_user_model().objects.get_or_create(username='verification-construction',
                                                         defaults={'is_staff': True, 'is_superuser': True})
    client = Client()
    client.force_login(testeur)
    for chemin in ['/', '/entree/', '/sortie/', '/articles/', '/articles/nouveau/', '/bons/', '/recherche/',
                   '/recherche/?q=filtre', '/tableau-de-bord/', '/consommation/', '/reglages/ia/', '/reglages/societe/',
                   '/reglages/plan/', '/reglages/plan/editeur/', '/reglages/listes/', '/reglages/utilisateurs/', '/activation/', '/admin/',
                   '/reglages/sauvegarde/', '/reglages/exemple/effacer/', '/reglages/telephones/']:
        code = client.get(chemin).status_code
        assert code == 200, f'{chemin} → {code}'
        lignes.append(f'OK écran {chemin}')
    import ssl

    from stock import mises_a_jour

    fichier = settings.DATA_DIR / mises_a_jour.FICHIER
    ancien = fichier.read_bytes() if fichier.exists() else None
    fichier.write_text(json.dumps({'version': '999.0.0', 'url': 'https://exemple.com/MagaStock-Installation.exe',
                                   'notes': 'Essai'}), encoding='utf-8')
    try:
        page = client.get('/').content.decode()
        assert 'Nouvelle version 999.0.0' in page and 'Vérifier les mises à jour' in client.get(
            '/reglages/sauvegarde/').content.decode(), 'carte de mise à jour absente'
    finally:
        if ancien is None:
            fichier.unlink()
        else:
            fichier.write_bytes(ancien)
    ssl.create_default_context()  # HTTPS disponible pour lire version.json
    lignes.append(f'OK mises à jour ({ssl.OPENSSL_VERSION}, {produit.MAJ_URL})')
    testeur.delete()
    from stock import reseau

    lignes.append(f'OK QR code ({len(reseau.qr_svg("http://192.168.1.20:8765/"))} octets)')
    port = reseau.demarrer()  # accès des téléphones : second serveur ouvert sur le réseau
    try:
        for hote, attendu in [(f'192.168.1.20:{port}', 200), ('exemple.com', 400)]:
            requete = urllib.request.Request(f'http://127.0.0.1:{port}/connexion/', headers={'Host': hote})
            try:
                code = urllib.request.urlopen(requete, timeout=20).status
            except urllib.error.HTTPError as e:
                code = e.code
            assert code == attendu, f'accès téléphones, {hote} → {code}'
        lignes.append(f'OK accès téléphones (port {port})')
    finally:
        reseau.arreter()
    from stock import licence

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey  # noqa: F401  (clés de licence)
    lignes.append(f'OK licence (code machine {licence.code_machine()})')
    import webview  # noqa: F401  (la bibliothèque de la fenêtre est bien incluse)
    lignes.append('OK fenêtre')
    (dossier / 'verification.txt').write_text('\n'.join(lignes) + '\nVérification réussie.\n', encoding='utf-8')


def signaler(dossier, message):
    """Sans console, on écrit l'erreur dans un fichier."""
    (dossier / 'erreur-demarrage.txt').write_text(message, encoding='utf-8')
    if '--test' in sys.argv or sys.platform != 'win32':
        print(message, file=sys.stderr)


def ecran_chargement(message='Démarrage…', erreur=False):
    couleur = '#dc2626' if erreur else produit.COULEUR
    rond = '' if erreur else '<div class="rond"></div>'
    return f'''<!doctype html><html><head><meta charset="utf-8"><style>
      html,body{{margin:0;height:100%;font-family:"Segoe UI",system-ui,sans-serif;background:#f3f5f7;color:#16202a}}
      .c{{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px;text-align:center;padding:24px}}
      .nom{{font-size:34px;font-weight:700;color:{produit.COULEUR};letter-spacing:-.02em}}
      .msg{{color:{couleur};max-width:640px;white-space:pre-wrap}}
      .rond{{width:34px;height:34px;border:4px solid #dbe2ea;border-top-color:{produit.COULEUR};border-radius:50%;
             animation:t .8s linear infinite}}@keyframes t{{to{{transform:rotate(360deg)}}}}
    </style></head><body><div class="c"><div class="nom">{produit.NOM}</div>{rond}<div class="msg">{html.escape(message)}</div></div></body></html>'''


def main():
    if '--test' not in sys.argv and not instance_unique():
        return  # déjà ouvert : sa fenêtre a été ramenée devant
    dossier = dossier_donnees()
    if '--test' in sys.argv:
        try:
            serveur, url = demarrer(dossier)
            verifier(url, dossier)
            serveur.close()
        except Exception:
            signaler(dossier, traceback.format_exc())
            sys.exit(1)
        return

    import webview

    try:
        webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER'] = True  # liens target=_blank (« Télécharger ») : navigateur
    except Exception:
        pass
    fenetre = webview.create_window(produit.NOM, html=ecran_chargement(), width=1400, height=900,
                                    min_size=(1000, 650), maximized=True, text_select=True, zoomable=True)
    etat = {}

    def afficher(message):
        try:
            fenetre.load_html(ecran_chargement(message))
        except Exception:
            pass

    def en_arriere_plan():
        try:
            etat['serveur'], url = demarrer(dossier, afficher)
            fenetre.load_url(url)
            threading.Thread(target=acces_telephones, daemon=True).start()
        except Exception as e:
            signaler(dossier, traceback.format_exc())
            cause = f'{e}\n\n' if isinstance(e, ErreurDemarrage) else ''
            fenetre.load_html(ecran_chargement(
                f"Le logiciel n'a pas pu démarrer.\n{cause}Détails enregistrés dans :\n"
                f"{dossier / 'erreur-demarrage.txt'}", erreur=True))
            return
        threading.Thread(target=sauvegarde_de_fond, daemon=True).start()
        threading.Thread(target=mises_a_jour_de_fond, daemon=True).start()

    icone = Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / 'bureau' / 'icone.ico'
    try:
        webview.start(en_arriere_plan, gui='edgechromium' if sys.platform == 'win32' else None, private_mode=False,
                      storage_path=str(dossier / 'fenetre'), icon=str(icone) if icone.exists() else None)
    except Exception:
        signaler(dossier, "La fenêtre ne peut pas s'ouvrir. Installez « Microsoft Edge WebView2 Runtime » (gratuit) : "
                          'https://go.microsoft.com/fwlink/p/?LinkId=2124703 puis relancez le logiciel.\n\n'
                 + traceback.format_exc())
        if sys.platform == 'win32':
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None, "La fenêtre ne peut pas s'ouvrir.\n\nInstallez « Microsoft Edge WebView2 Runtime » (gratuit, "
                      'site de Microsoft) puis relancez le logiciel.', produit.NOM, 0x10)
        sys.exit(1)
    if 'serveur' in etat:
        try:
            from stock import reseau

            reseau.arreter()
        except Exception:
            pass
        etat['serveur'].close()


if __name__ == '__main__':
    main()
