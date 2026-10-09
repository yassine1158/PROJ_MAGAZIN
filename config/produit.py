"""Identité du produit (logiciel vendu à plusieurs sociétés).

Le nom, le logo et la couleur de chaque société cliente se règlent dans le logiciel
(Réglages › Ma société) ; ici, seulement ce qui appartient au produit lui-même.
"""
NOM = 'MagaStock'
SLOGAN = 'Gestion de magasin et de stock'
VERSION = '1.5.0'
COULEUR = '#2457d6'  # couleur par défaut, modifiable par chaque société

# --- Vente -----------------------------------------------------------------
PRIX = '200 000 FCFA'          # licence pour 1 ordinateur, payée une fois
ESSAI_JOURS = 30               # essai gratuit complet, puis le logiciel demande une clé d'activation
WAVE_LIEN = ''                 # lien de paiement Wave Business (à coller ici), ex. https://pay.wave.com/…
WHATSAPP = ''                  # numéro pour recevoir la preuve de paiement, ex. 2250700000000 (sans + ni espaces)
SITE_WEB = 'https://yassine1158.github.io/PROJ_MAGAZIN/'

# --- Version Windows ---------------------------------------------------------
# VERSION ci-dessus doit être le numéro publié (windows.yml refuse de publier sinon).
# Fichier publié avec chaque version, lu par le logiciel pour signaler les nouvelles versions.
MAJ_URL = 'https://github.com/yassine1158/PROJ_MAGAZIN/releases/latest/download/version.json'
# Verrou Windows : une seule copie ouverte à la fois (même nom dans bureau/installateur.iss, AppMutex).
MUTEX = 'MagaStockInstance'

# Clé publique de vérification des licences (créée avec « outils/licences.py cles »).
# Vide : pas de verrouillage (le logiciel reste libre, utile pour les essais et les tests).
CLE_PUBLIQUE = ''
