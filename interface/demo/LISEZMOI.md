# Écran de démo : la salle d'enquête

Maquette non officielle, faite pour le hackathon « Hack the Feed ». Le logo Brut. n'y figure qu'à titre de maquette.

## Lancer
1. Le serveur d'enquête, depuis la racine du dépôt (port 8802) :
   ```
   python -m enquete.serveur --racine <dossier des enquêtes> --port 8802
   ```
2. L'écran, depuis ce dossier (port 8795) :
   ```
   python serve.py
   ```
Puis ouvrir http://127.0.0.1:8795/.

## Le parcours (cas fictif « Stylos minceur », sans serveur)
- La salle : les écrans tournent. Entrée ou clic : la caméra entre dans l'écran mural et le réseau se construit.
- Clic sur un rôle : ses pièces et le résumé d'une pièce. « Relais » : 47 vidéos qui répètent la même phrase, rangées en 6 sources.
- « Comparer » : la preuve, deux vidéos sur une seule tête de lecture. La bannière rouge mène à une zone d'ombre et à ses pistes.
- Clic sur la question : le résumé de l'enquête. Échap remonte d'un niveau.

## Un vrai cas (enquête Brut OpenAI / Hugging Face)
Avec le serveur d'enquête lancé : http://127.0.0.1:8795/?flux&enquete=openai-agents-huggingface . Le réseau vient tout entier de l'export de l'enquête (`reseau.html`) : rôles, compteurs, images des sources (`vignettes-<enquete>.json`), zones d'ombre et ligne des sources lues. `&mode=direct` suit une enquête en cours.

## Paramètres
- `?base=http://127.0.0.1:8802` : adresse du serveur d'enquête (8802 par défaut).
- `?verif` (sur `jet-d-plan-sequence.html`) : vérification automatique des chevauchements, rapport dans le titre de la page.

Cas fictif, données d'exemple et images d'illustration générées : aucune personne réelle n'est visée.
