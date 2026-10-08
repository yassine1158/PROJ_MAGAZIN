"""Version bureau du logiciel (Windows).

Ouvre le logiciel dans sa propre fenêtre (sans navigateur ni fenêtre noire). Un écran de chargement
s'affiche tout de suite, pendant que le logiciel démarre en arrière-plan.
Les données sont gardées dans %LOCALAPPDATA%\\<nom du produit> (base, photos, clés).

    python bureau.py           ouvre le logiciel
    python bureau.py --test    vérifie que tout démarre, sans ouvrir de fenêtre (utilisé à la construction)
"""
import os
import shutil
import socket
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path

from config import produit

ANCIENS_DOSSIERS = ['Magasin SI BETON']  # versions précédentes : les données sont reprises


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


def preparer(dossier):
    os.environ['MAGASIN_BUREAU'] = '1'
    os.environ['MAGASIN_DATA_DIR'] = str(dossier)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django

    django.setup()
    from django.core.management import call_command
    from django.core.management.commands.migrate import Command as Migrate
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    executeur = MigrationExecutor(connection)
    if executeur.migration_plan(executeur.loader.graph.leaf_nodes()):  # base à créer ou à mettre à jour
        call_command(Migrate(), interactive=False, verbosity=0)


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


def demarrer(dossier):
    preparer(dossier)
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
                   '/tableau-de-bord/', '/consommation/', '/reglages/ia/', '/reglages/societe/',
                   '/reglages/plan/', '/reglages/plan/editeur/', '/reglages/listes/', '/reglages/utilisateurs/', '/activation/', '/admin/']:
        code = client.get(chemin).status_code
        assert code == 200, f'{chemin} → {code}'
        lignes.append(f'OK écran {chemin}')
    testeur.delete()
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
    </style></head><body><div class="c"><div class="nom">{produit.NOM}</div>{rond}<div class="msg">{message}</div></div></body></html>'''


def main():
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

    fenetre = webview.create_window(produit.NOM, html=ecran_chargement(), width=1400, height=900,
                                    min_size=(1000, 650), maximized=True, text_select=True, zoomable=True)
    etat = {}

    def en_arriere_plan():
        try:
            etat['serveur'], url = demarrer(dossier)
            fenetre.load_url(url)
        except Exception:
            signaler(dossier, traceback.format_exc())
            fenetre.load_html(ecran_chargement(
                "Le logiciel n'a pas pu démarrer.\nDétails enregistrés dans :\n"
                f"{dossier / 'erreur-demarrage.txt'}", erreur=True))

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
        etat['serveur'].close()


if __name__ == '__main__':
    main()
