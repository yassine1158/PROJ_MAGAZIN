"""Outil du vendeur : créer ses clés une fois, puis une clé d'activation pour chaque client.

    python outils/licences.py            menu (double-clic : licences.bat)
    python outils/licences.py cles       créer la paire de clés (une seule fois)
    python outils/licences.py creer      créer une clé d'activation pour un client

La clé PRIVÉE est gardée dans  <Documents>/MagaStock-vendeur/cle-privee.txt  : ne la donnez à personne,
ne la mettez pas sur GitHub, faites-en une copie de sauvegarde (clé USB). Sans elle, impossible de
créer de nouvelles licences ; si quelqu'un la vole, il peut en créer.
La clé PUBLIQUE va dans config/produit.py (CLE_PUBLIQUE) : elle peut être vue par tout le monde.
"""
import base64
import csv
import sys
from datetime import date
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402


def dossier():
    documents = Path.home() / 'Documents'
    d = (documents if documents.is_dir() else Path.home()) / 'MagaStock-vendeur'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _b64(octets):
    return base64.urlsafe_b64encode(octets).decode().rstrip('=')


def creer_cles():
    fichier = dossier() / 'cle-privee.txt'
    if fichier.exists():
        print(f'Vos clés existent déjà : {fichier}\n(Ne les recréez pas : les licences déjà vendues ne marcheraient plus.)')
        return montrer_cle_publique()
    privee = Ed25519PrivateKey.generate()
    brut = privee.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    fichier.write_text(_b64(brut))
    print(f'Clé privée enregistrée : {fichier}')
    print('>> Faites-en une copie sur une clé USB. Ne la partagez jamais.\n')
    montrer_cle_publique()


def charger_privee():
    fichier = dossier() / 'cle-privee.txt'
    if not fichier.exists():
        sys.exit("Pas encore de clés : choisissez d'abord « Créer mes clés ».")
    brut = base64.urlsafe_b64decode(fichier.read_text().strip() + '==')
    return Ed25519PrivateKey.from_private_bytes(brut)


def montrer_cle_publique():
    publique = charger_privee().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    print('Clé publique (à mettre dans config/produit.py, ligne CLE_PUBLIQUE) :\n')
    print(f"CLE_PUBLIQUE = '{_b64(publique)}'\n")


def creer_licence(client=None, machine=None):
    from stock.licence import signer  # même format que le logiciel

    client = client or input('Nom du client (société) : ').strip()
    machine = (machine or input("Code de l'ordinateur du client (ex. 7F3A-91C2) : ")).strip().upper()
    if not client or not machine:
        sys.exit('Nom et code obligatoires.')
    cle = signer(charger_privee(), client, machine)
    registre = dossier() / 'licences-vendues.csv'
    nouveau = not registre.exists()
    with registre.open('a', newline='', encoding='utf-8') as f:
        ecrivain = csv.writer(f, delimiter=';')
        if nouveau:
            ecrivain.writerow(['date', 'client', 'ordinateur', 'cle'])
        ecrivain.writerow([date.today().isoformat(), client, machine, cle])
    print(f"\nClé d'activation pour « {client} » ({machine}) — à envoyer au client :\n\n{cle}\n")
    print(f'(Gardée aussi dans {registre})')
    return cle


def menu():
    print('=== Licences MagaStock ===\n1. Créer mes clés (une seule fois)\n2. Créer une licence pour un client\n'
          '3. Afficher ma clé publique\n')
    choix = input('Votre choix : ').strip()
    {'1': creer_cles, '2': creer_licence, '3': montrer_cle_publique}.get(choix, lambda: print('Choix inconnu.'))()


if __name__ == '__main__':
    commande = sys.argv[1] if len(sys.argv) > 1 else ''
    {'cles': creer_cles, 'creer': creer_licence, 'publique': montrer_cle_publique}.get(commande, menu)()
