# MagaStock — gestion de magasin, recherche IA et plan 3D

Logiciel de gestion de magasin et de stock (pièces de rechange, lubrifiants, pneus, outillage, EPI, matériaux…),
prévu pour être utilisé par plusieurs sociétés : chacune règle son nom, son logo et sa couleur dans
*Réglages › Ma société*. Le nom du produit se change dans `config/produit.py`.

**L'idée :** on écrit ce qu'on cherche comme on le dit (« filtre zit volvo », « smenta », « 7assira »…)
ou on **prend une photo** de la pièce. Le logiciel trouve l'article et montre **où il est rangé**
(Bloc › Étagère › Niveau) dans une **vue 3D du magasin** : l'étagère passe en rouge, le niveau clignote
et la caméra s'y déplace.

## Fonctions

| | |
|---|---|
| **Recherche IA** | Texte en français, darija ou arabe, avec fautes, ou photo de la pièce (API Anthropic). Sans clé IA, une recherche simple tolérante aux fautes fonctionne quand même. |
| **Plan 3D** | Blocs, étagères et niveaux dessinés à partir du plan saisi dans l'administration. Fonctionne sans internet (three.js inclus). |
| **Articles** | Code, désignation, catégorie, unité, emplacement, photo, mots-clés, stock minimum. |
| **Bons d'entrée** | Réception fournisseur : le stock augmente et le **prix moyen pondéré** est recalculé. |
| **Bons de sortie** | Vers un chantier et/ou un engin. Refus si le stock est insuffisant. |
| **Inventaire** | Saisie du stock compté : le logiciel corrige le stock et garde l'écart. |
| **Annulation** | Un bon validé ne se supprime pas : on l'annule, le stock est remis en place. |
| **Historique** | Chaque mouvement est enregistré (qui, quand, quel bon, stock après). |
| **Alertes** | Articles sous le stock minimum, ruptures. |
| **Rapports** | Tableau de bord, état du stock, consommation par chantier, export **Excel**, bons en **PDF**. |

## Réglages (menu de gauche, comptes « Responsable »)

- **Plan du magasin** : blocs (zones) et étagères, avec vue de dessus ; « Ajouter une rangée » crée plusieurs
  étagères d'un coup. **Dessiner le plan** ouvre l'éditeur graphique : murs, blocs et étagères à la souris
  (glisser pour déplacer, coins pour agrandir, grille de 50 cm, annuler, dupliquer, tourner), avec un onglet Vue 3D.
- **Chantiers, engins…** : listes proposées dans les entrées et sorties (chantiers, engins, fournisseurs, catégories).
- **Utilisateurs** : un compte par personne ; *Magasinier* (entrées, sorties, articles) ou *Responsable* (tout).
- **Ma société** : nom, logo, couleur, adresse, devise, texte de signature des bons.
- **Recherche IA** : clé de l'API Anthropic.

## Logiciel Windows (recommandé)

1. Sur GitHub, ouvrir **Releases** (colonne de droite) et télécharger **Installer-MagaStock-….exe**.
2. Double-cliquer dessus. Si Windows affiche « Windows a protégé votre ordinateur » : *Informations
   complémentaires* › *Exécuter quand même* (le programme n'est pas signé numériquement).
3. Ouvrir **MagaStock** depuis l'icône du bureau. Au premier lancement : nom et logo de la société, puis compte du responsable.

- Le logiciel s'ouvre dans sa propre fenêtre, sans navigateur, et fonctionne sans internet.
- Les données sont dans `%LOCALAPPDATA%\MagaStock` (base `db.sqlite3`, photos). Pour une **sauvegarde**,
  copier ce dossier. Elles sont gardées lors d'une mise à jour ou d'une désinstallation.
- Les PDF et fichiers Excel sont enregistrés dans `Documents\MagaStock` et s'ouvrent directement.
- Recherche IA : menu *Réglages › Recherche IA*, coller la clé.
- Nécessite Windows 10 ou 11 (avec « Microsoft Edge WebView2 Runtime », déjà présent sur la plupart des PC).

**Publier une nouvelle version :** sur GitHub, onglet *Actions* › *Logiciel Windows* › *Run workflow*, indiquer
le numéro (ex. `1.1.0`). GitHub construit le programme sur Windows, le vérifie et met l'installateur dans *Releases*
(pousser un tag `vX.Y.Z` fonctionne aussi)
(fichiers : `bureau.py`, `bureau/`, `.github/workflows/windows.yml`).

## Démarrage rapide sans installateur (Windows)

1. Installer **Python** depuis https://www.python.org/downloads/ en cochant **« Add python.exe to PATH »**.
2. Sur GitHub : bouton vert **Code › Download ZIP**, puis décompresser le dossier.
3. Double-cliquer sur **`lancer.bat`**.
   - La première fois : installation automatique (quelques minutes), puis création du compte
     administrateur (nom, e-mail facultatif, mot de passe) et proposition d'installer un magasin d'exemple.
   - Le logiciel s'ouvre dans le navigateur sur http://127.0.0.1:8000.
4. Les fois suivantes : double-clic sur `lancer.bat`, se connecter, c'est tout. Laisser la fenêtre noire ouverte
   pendant l'utilisation.

Sous Linux / macOS : `./lancer.sh`.

## Installation manuelle

```bash
python -m venv venv
source venv/bin/activate          # Windows : venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py demo             # facultatif : magasin d'exemple (4 blocs, 18 articles)
python manage.py runserver
```

Ouvrir http://127.0.0.1:8000 et se connecter.

### Activer l'IA

1. Créer une clé sur https://console.anthropic.com (rubrique *API keys*).
2. La mettre dans l'environnement avant de lancer le serveur :
   ```bash
   export ANTHROPIC_API_KEY=sk-ant-...
   ```
   Sur PythonAnywhere : onglet *Web* → fichier WSGI, ajouter `os.environ['ANTHROPIC_API_KEY'] = '...'`.

Le modèle se change avec `MAGASIN_IA_MODELE` (par défaut `claude-opus-5-5`). Chaque recherche IA est un appel
payant à l'API (quelques centimes au plus) ; la recherche simple, elle, est gratuite.

### Production

Variables d'environnement : `DJANGO_SECRET_KEY` (obligatoire), `DJANGO_DEBUG=0`,
`DJANGO_ALLOWED_HOSTS=votre-domaine`, et facultativement `MAGASIN_SOCIETE`, `MAGASIN_DEVISE` (FCFA par défaut),
`MAGASIN_SIGNATAIRE`. Puis `python manage.py collectstatic`. Servir `/media/` (photos des articles) depuis le
serveur web.

## Mise en route

1. **Administration › Blocs** : créer les blocs (A, B, C…) avec leur position et leurs dimensions en mètres
   (x vers la droite, z vers le fond), puis leurs étagères (position dans le bloc, taille, nombre de niveaux).
2. **Administration › Articles** : créer les articles, choisir l'étagère et le niveau, ajouter une photo et
   les noms utilisés au magasin dans *mots-clés* (ex. « filtre zit, filtre huile »).
3. **Bon d'entrée** (stock initial ou réception) → action *Valider*.
4. **Bon de sortie** pour chaque sortie → action *Valider*, puis *Imprimer (PDF)*.

## Organisation du code

```
config/            paramètres Django
stock/models.py    blocs, étagères, articles, bons, mouvements
stock/services.py  règles du stock (validation, annulation, prix moyen) – seul endroit qui modifie le stock
stock/recherche.py recherche locale + interprétation IA (texte et photo)
stock/static/stock/magasin3d.js   vue 3D (three.js)
stock/tests.py     tests : python manage.py test stock
```

Licence de three.js : MIT (`stock/static/stock/vendor/three/LICENSE`).
