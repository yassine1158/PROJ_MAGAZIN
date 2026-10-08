# Construction de la version bureau : pyinstaller bureau/magasin.spec
import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

RACINE = os.path.abspath(os.path.join(SPECPATH, '..'))
sys.path.insert(0, RACINE)
from config import produit  # noqa: E402

NOM = produit.NOM

datas, binaries, hiddenimports = [], [], []
for paquet in ['django', 'jazzmin', 'reportlab', 'openpyxl', 'PIL', 'anthropic', 'pydantic', 'whitenoise',
               'waitress', 'webview', 'clr_loader', 'pythonnet']:
    try:
        d, b, h = collect_all(paquet)
    except Exception:
        continue  # paquet absent sur cette plateforme
    datas += d
    binaries += b
    hiddenimports += h

hiddenimports += collect_submodules('stock') + collect_submodules('config')
# Le code du logiciel est aussi copié tel quel : Django y trouve ses gabarits, fichiers et migrations.
datas += [
    (os.path.join(RACINE, 'stock'), 'stock'),
    (os.path.join(RACINE, 'config'), 'config'),
    (os.path.join(SPECPATH, 'icone.ico'), 'bureau'),
]

a = Analysis(
    [os.path.join(RACINE, 'bureau.py')],
    pathex=[RACINE],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=['tkinter', 'matplotlib', 'IPython', 'pytest'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name=NOM,
    icon=os.path.join(SPECPATH, 'icone.ico'),
    console=False,  # pas de fenêtre noire
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name=NOM, upx=False)
