# Banc d'essai

Le banc d'essai répond à une question : l'outil retrouve-t-il ce que la presse a établi sur une affaire déjà résolue, sans rien inventer ?

On prend une affaire dont on connaît la réponse (le **corrigé**), on laisse l'outil enquêter (le **rendu**), puis on compare. Le résultat tient en dix lignes, lisibles sans connaissance technique.

## Lancer une comparaison

```
python -m enquete.banc banc/affaires/exemple-fictif.json mon-rendu.json
```

Les corrigés vivent dans `banc/affaires/`. `exemple-fictif.json` est inventé de bout en bout : il sert aux tests.

Autres commandes :

```
python -m enquete.banc corrige.json rendu.json journal-collecte.json   # preuves rapprochées de la collecte
python -m enquete.banc --controle banc/affaires/*.json                 # fait sans portée, sans source, seuil lâche, canari absent
python -m enquete.banc --truquages banc/affaires/*.json                # rappel de quatre rendus de triche
python -m enquete.banc --garde-fou donnees/garde-fou.json banc/affaires/*.json   # canaris et URL de presse à bloquer
```

## Le corrigé (un fichier par affaire)

| Champ | Sens |
|---|---|
| `id` | nom court de l'affaire, identique dans le rendu |
| `question` | la question posée à l'outil |
| `gel_donnees` | date après laquelle on ne regarde plus rien (AAAA-MM-JJ) |
| `reserve` | `true` : affaire gardée pour le test final, jouée une seule fois (voir plus bas) |
| `canari` | phrase absurde, propre à l'affaire : si elle apparaît dans un rendu ou une trace d'agent, le corrigé a fuité |
| `faits` | les faits que l'outil doit retrouver |
| `non_etabli` | ce que la presse dit ne pas savoir : l'outil ne doit pas l'affirmer |
| `pieges` | affirmations à ne pas produire (chiffres de périmètres différents, coupable désigné trop vite…) |

Un fait attendu porte :

- `id` : « F1 », « F2 »… ;
- `phrase` : le fait, en une phrase ;
- `type` : `origine` (la première occurrence), `reprise`, `acteur`, `role`, `lien`, `chronologie`, `interlocuteur` ou `zone_ombre` (ce que les données ne montrent pas) ;
- `poids` : l'importance, 1 par défaut (un fait d'origine pèse plus qu'un détail) ;
- `portee` : ce que l'outil peut aller chercher. `video` (visible dans une vidéo : image, parole, texte à l'écran, légende, date ou audience de la vidéo), `web` (se lit seulement dans la presse, un registre, un site officiel, un message de X), `hors_portee` (aucune source ouverte indexée : déclaration privée, interview, ce que les données ne montrent pas) ;
- `source` et `autres_sources` : les articles ou documents qui établissent le fait ;
- `correspondance` : comment reconnaître le fait dans un texte. `mots_cles` liste les mots à retrouver, `seuil` la part qu'il faut en retrouver (0,6 = six sur dix, minimum accepté pour un fait). Sans `mots_cles`, ce sont les mots de la phrase qui servent.

Les éléments de `non_etabli` et de `pieges` ont `id`, `phrase` et le même bloc `correspondance`.

## Le rendu de l'outil

```json
{
  "affaire_id": "exemple-fictif",
  "faits": [
    {
      "id": "p1",
      "texte": "La première vidéo date du 3 mars…",
      "preuves": [{"url": "https://…", "extrait": "passage qui prouve le fait"}],
      "fait_vise": "F1"
    }
  ]
}
```

- `preuves` : au moins une URL **et** un extrait. Sans les deux, le fait est compté comme inventé.
- `fait_vise` : facultatif. Si l'outil sait quel fait attendu il vise, la correspondance est directe, **à condition que le texte du fait dise bien ce fait attendu** (au seuil du fait visé). Sinon l'identifiant est refusé (`vise_refuses`) et le texte seul décide.
- `statut` : mettre `"non_etabli"` quand l'outil dit honnêtement « on ne sait pas ». Ce n'est ni un succès ni une erreur : un aveu.

## Comment le score se calcule

- **Rappel pondéré** : part des faits attendus retrouvés, chacun compté selon son poids.
- **Précision** : part des faits affirmés par l'outil qui correspondent à un fait attendu. Un fait vrai mais absent du corrigé n'est pas compté juste : le chiffre est une borne basse, la liste `hors_corrige` dit lesquels relire.
- **Rappel sur le visible et sur le total** : le total compte tous les faits attendus ; le visible ne compte que ceux de portée `video` ou `web`. L'écart mesure ce que l'outil ne pouvait pas voir. `par_portee` détaille vidéo, web et hors portée.
- **Fait inventé** : sans preuve, ou dont les preuves sont refusées, ou qui affirme un piège, ou qui établit un point du `non_etabli`. Il ne compte jamais dans le rappel.
- **Correspondance** : d'abord par `fait_vise` (confirmé par le texte) ; sinon par les mots (sans accents ni majuscules, mots courants ignorés). Ces rapprochements sont signalés « à relire par un humain » : le score ne fait pas foi tant qu'ils ne sont pas relus.

## Contre la triche

- **Journal de collecte** : liste JSON de `{"url", "texte"}`, ce que l'outil a réellement lu pendant le passage (transcriptions, pages). Avec lui, une preuve n'est retenue que si son URL y figure et si son extrait s'y retrouve mot pour mot. Sans lui, le rapport dit « ce chiffre ne fait pas foi » : une recopie des phrases du corrigé avec de fausses preuves passerait.
- **Phrase canari** : si elle figure dans un fait ou un extrait du rendu, l'évaluation est refusée (`FuiteCorrige`). Le garde-fou doit aussi la chercher dans les traces des agents.
- **Liste noire** : toute URL citée dans le corrigé (sources, notes) est exportée par `--garde-fou` pour être interdite aux agents ; une preuve qui la cite est refusée.
- **Rendus truqués** : `--truquages` rejoue quatre triches (identifiants sans texte, recopie du corrigé, preuves tirées de la presse du corrigé, canari). Avec un journal de collecte vide, les quatre tombent à 0 % sur les cinq affaires d'entraînement.

## Affaire de réserve

Une affaire marquée `"reserve": true` ne peut être évaluée qu'une seule fois, et seulement avec un journal de collecte.

- Le journal est **hors du dépôt** : `~/.hack-oriane/banc-reserve.jsonl` (ou le fichier de la variable `HACK_ORIANE_RESERVE`). L'ancien `donnees/banc-reserve.jsonl` est encore lu pour refuser un rejeu.
- Chaque ligne porte l'empreinte de la ligne précédente : une ligne modifiée ou retirée casse la chaîne et l'évaluation est refusée (`JournalAltere`).
- `sceller_reserve(corrige)` note l'empreinte du corrigé avant le travail des agents ; si le corrigé change ensuite, l'évaluation est refusée.
- Un second passage est refusé par identifiant **et** par empreinte du contenu : renommer l'affaire n'aide pas. Une fuite du canari sur la réserve la consomme aussi.
- Un passage refusé faute de journal de collecte ne consomme rien.

Règle du jeu : on règle l'outil sur les autres affaires, puis on joue la réserve une fois, et c'est ce chiffre qu'on montre. Effacer le journal pour rejouer rend le chiffre sans valeur.

Réserve actuelle : `affaire-09-stylos-glp1`. Aucune autre réserve n'est écrite pour l'instant (décision d'Adem sur le sujet de démo en attente).
