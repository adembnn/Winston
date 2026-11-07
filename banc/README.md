# Banc d'essai

Le banc d'essai répond à une question : l'outil retrouve-t-il ce que la presse a établi sur une affaire déjà résolue, sans rien inventer ?

On prend une affaire dont on connaît la réponse (le **corrigé**), on laisse l'outil enquêter (le **rendu**), puis on compare. Le résultat tient en dix lignes, lisibles sans connaissance technique.

## Lancer une comparaison

```
python -m enquete.banc banc/affaires/exemple-fictif.json mon-rendu.json
```

Les corrigés vivent dans `banc/affaires/`. Le seul fichier fourni ici, `exemple-fictif.json`, est inventé de bout en bout : il sert aux tests.

## Le corrigé (un fichier par affaire)

| Champ | Sens |
|---|---|
| `id` | nom court de l'affaire, identique dans le rendu |
| `question` | la question posée à l'outil |
| `gel_donnees` | date après laquelle on ne regarde plus rien (AAAA-MM-JJ) |
| `reserve` | `true` : affaire gardée pour le test final, jouée une seule fois (voir plus bas) |
| `faits` | les faits que l'outil doit retrouver |
| `non_etabli` | ce que la presse dit ne pas savoir : l'outil ne doit pas l'affirmer |
| `pieges` | affirmations à ne pas produire (chiffres de périmètres différents, coupable désigné trop vite…) |

Un fait attendu porte :

- `id` : « F1 », « F2 »… ;
- `phrase` : le fait, en une phrase ;
- `type` : `origine` (la première occurrence), `reprise`, `acteur`, `role`, `lien`, `chronologie`, `interlocuteur` ou `zone_ombre` (ce que les données ne montrent pas) ;
- `poids` : l'importance, 1 par défaut (un fait d'origine pèse plus qu'un détail) ;
- `correspondance` : comment reconnaître le fait dans un texte. `mots_cles` liste les mots à retrouver, `seuil` la part qu'il faut en retrouver (0,6 = six sur dix). Sans `mots_cles`, ce sont les mots de la phrase qui servent.

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
- `fait_vise` : facultatif. Si l'outil sait quel fait attendu il vise, la correspondance est directe.
- `statut` : mettre `"non_etabli"` quand l'outil dit honnêtement « on ne sait pas ». Ce n'est ni un succès ni une erreur : un aveu.

## Comment le score se calcule

- **Rappel pondéré** : part des faits attendus retrouvés, chacun compté selon son poids.
- **Précision** : part des faits affirmés par l'outil qui correspondent à un fait attendu. Un fait vrai mais absent du corrigé n'est pas compté juste : le chiffre est une borne basse, la liste `hors_corrige` dit lesquels relire.
- **Fait inventé** : sans preuve, ou qui affirme un piège, ou qui établit un point du `non_etabli`. Il ne compte jamais dans le rappel.
- **Correspondance** : d'abord par `fait_vise` ; sinon par les mots (sans accents ni majuscules, mots courants ignorés). Ces rapprochements sont signalés « à relire par un humain » : le score ne fait pas foi tant qu'ils ne sont pas relus.

## Affaire de réserve

Une affaire marquée `"reserve": true` ne peut être évaluée qu'une seule fois. La première évaluation écrit une trace dans `donnees/banc-reserve.jsonl` (dossier ignoré par git) ; toute seconde tentative est refusée. Règle du jeu : on règle l'outil sur les autres affaires, puis on joue la réserve une fois, et c'est ce chiffre qu'on montre. Effacer le journal pour rejouer rend le chiffre sans valeur.
