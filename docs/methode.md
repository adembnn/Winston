# Méthode

Ce document décrit comment l'outil raisonne, et ce qu'il s'interdit. Les mécanismes décrits sont généraux : aucune consigne d'agent ne contient de nom, de date ou de fait tiré d'une affaire du banc d'essai.

## 1. Le point de départ

Le journaliste donne deux choses :

- **une question** (« Stylos minceur : qui est derrière ? », comme on dirait « Panama Papers ») ;
- **une graine**, facultative : une vidéo envoyée par un abonné, une capture d'écran, un lien, le récit d'une arnaque vécue.

L'outil tire de la graine des ancres (pseudo, phrase exacte, produit, lien en bio, montant, lieu). Il choisit un ou deux gabarits d'affaire, puis formule trois ou quatre hypothèses concurrentes, dont une qui donne tort au journaliste. Pour chacune, il écrit ce qui la ferait tomber. Il cherche d'abord à réfuter l'hypothèse dominante.

Sans graine, la question seule suffit : la collecte part de ses mots.

## 2. Les gabarits et les questions ouvertes

Un gabarit est la liste de contrôle d'un enquêteur chevronné : devant une arnaque, il lui faut le vendeur, le chemin de l'argent, les victimes, l'autorité qui a déjà agi. Chaque case vide devient une question ouverte numérotée, avec deux ou trois pistes (où chercher, par quelle source, à quel coût). La liste existe donc avant la première recherche.

| Gabarit | Ce qu'il oblige à chercher |
|---|---|
| Arnaque commerciale | première annonce, qui encaisse, chemin lien en bio → site → mentions légales → société, décision d'autorité |
| Désinformation santé | l'affirmation mot pour mot, qui vend le remède, ce qu'en dit l'agence sanitaire, un expert qui répond déjà |
| Rumeur virale | la première occurrence trouvée, la bascule vers le massif, la mutation du récit, le démenti daté |
| Contenus recyclés | date et lieu de l'original, première reprise, légendes qui changent selon le pays |
| Fabrication (faux propos, montage, IA) | la vraie vidéo d'origine, ce qui a été changé, le démenti |
| Campagne coordonnée | gestes répétés à la même heure, même texte, infrastructure partagée |

Les rôles de base sont : à l'origine, amplificateurs, bénéficiaires, infrastructure, victimes, témoins, experts, contradicteurs, mis en cause, inconnus. Le gabarit dit lesquels sont attendus. Un gabarit libre (les dix rôles, sans faits attendus) sert quand rien ne colle.

**Règles des cases.**

- Une case ne se remplit que par une pièce qui a passé le contrôle de preuve. Une intuition devient une piste, rangée sous la question.
- Une question porte un texte, un rôle visé, des pistes, et un statut : *ouverte*, *en cours*, *comblée* (par la pièce nommée) ou *hors de portée* (« il faut un humain : voici qui peut voir »).
- Une contradiction entre deux pièces ouvre une question (« 550 000 ou 800 000 manifestants ? ») au lieu d'être tranchée.

## 3. Les six questions de l'enquêteur

Posées à chaque affaire, dans cet ordre :

1. **Qu'est-ce qui ne colle pas ?** Une date impossible (la vidéo est plus vieille que l'événement), un compte créé juste avant, la même phrase mot pour mot sur plusieurs comptes, des publications synchronisées à la minute, un lieu ou une langue qui ne correspondent pas à l'image.
2. **D'où ça vient ?** Le premier vu, et ce que l'on ne peut pas voir. Les plateformes hors de portée sont dites, pas ignorées.
3. **À qui ça profite ?** Quatre monnaies :
   - *argent* : vente directe, affiliation, code promo, lien en bio vers une boutique, rémunération aux vues, cagnotte, abonnement, contacts revendus ;
   - *audience* : croissance d'un compte, revente de compte, ferme d'engagement ;
   - *influence* : cause politique ou acteur étranger, jamais conclu sans source solide, jamais sur la seule langue ou le seul fuseau horaire ;
   - *nuisance* : nuire à un concurrent, une personne, une institution.

   Pour chaque monnaie retenue, il faut dire où passe la valeur, la pièce qui le montre et ce qui manque. Sans pièce, c'est une hypothèse.
4. **Qui fait tourner la machine, et où va l'argent ?** On suit l'infrastructure : lien en bio, domaine (date de création, hébergeur, domaines frères), boutique, code promo, pseudo réutilisé, mentions légales et SIREN, société, canal Telegram. On suit aussi les flux : adresses de portefeuilles crypto visibles dans une bio, une légende, le texte à l'écran ou un QR code ; transactions prouvées par leur identifiant sur un explorateur public ; portefeuilles dépensés ensemble ; liens de paiement et de cagnotte. Un portefeuille de plateforme d'échange, partagé par des milliers d'utilisateurs, ne fait qu'un lien faible. Un portefeuille n'est jamais attribué à une personne sans source solide.
5. **Quelle est l'affaire derrière l'affaire ?** L'affaire visible est souvent un symptôme. On monte en généralité de trois façons, chacune posée comme hypothèse avec le test qui la prouverait et celui qui la réfuterait : *le réseau* (les mêmes acteurs dans d'autres affaires), *le système* (le modèle économique qui rend l'affaire rentable), *la faille* (qui aurait dû l'empêcher, et pourquoi cela n'a pas suffi).
6. **Qui doit répondre, qui peut témoigner, qui a démenti ?** Les mis en cause, à contacter avant publication, les témoins directs, les experts, les contradicteurs.

## 4. Ce qui fait un lien intelligent

- **Typé.** Un lien dit une relation précise : reprend, même opérateur, même infrastructure, même argent, coordonné avec, profite à, contredit, recyclé, généré par IA. Jamais « lié à » tout court.
- **Prouvé.** Il porte son URL, son extrait exact et sa seconde, vérifiés par du code. Sans preuve, c'est une piste.
- **Pesé par la rareté.** Hashtag populaire, son tendance, gabarit de montage répandu : ces points communs ne prouvent rien. Même code promo, même domaine, même faute d'orthographe, même SIREN, même plan coupé à la même image : ils pèsent lourd. Le poids vaut à peu près −log de la fréquence de l'indice dans le corpus (modèle de Fellegi et Sunter).
- **Convergent.** Seuls s'additionnent des indices qui ne viennent pas du même geste. Une vidéo republiée compte pour un seul indice. Un indice contraire pèse en négatif ; une donnée manquante pèse zéro. L'outil affiche la force des indices, jamais une probabilité que le lien soit vrai.
- **Mis en concurrence.** Chaque lien fort a au moins une explication innocente, et l'indice qui les départage (voir §5).
- **Enchaîné.** Compte → lien en bio → boutique → société → autres boutiques → autres comptes. Une chaîne vaut son maillon le plus faible, et on l'affiche.
- **Chiffré quand c'est possible.** Combien de comptes, de vidéos, d'euros, sur combien de jours.

Échelle de lecture d'un lien, du plus faible au plus fort : indices contraires, coïncidence probable, indice faible, appui net, lien fort, lien très fort.

## 5. Les six explications à écarter

Devant deux contenus qui se ressemblent, l'outil écrit d'abord les explications innocentes, avec ce qui départage chacune :

| Explication | Ce qui la départage |
|---|---|
| Coïncidence | la rareté de l'indice dans le corpus |
| Tendance virale | le nombre de vidéos par jour sur ce motif |
| Même source citée | presse, recherche web, vidéos plus anciennes |
| Copie | le délai entre les deux, l'empreinte d'image |
| Même auteur | identité, lien en bio, infrastructure |
| Coordination | comparaison à un modèle nul (heures de publication au hasard) |

Le lien écrit nomme la gagnante et les écartées, avec la preuve qui les écarte. Il ajoute les explications propres au terrain (agence de créateurs, « challenge » de marque).

## 6. Deux notes séparées par pièce

Chaque pièce reçoit un rôle et deux notes, établies en deux appels distincts, le second sans voir le premier :

- **fiabilité de la source**, de A à F ;
- **crédibilité de l'information**, de 1 à 6.

Elles sont toujours traduites en mots (« source habituellement fiable ; information recoupée par d'autres sources indépendantes »). Règles mécaniques : un registre ou une décision d'autorité vaut A au mieux ; un compte anonyme ne dépasse jamais E ; « recoupée » exige deux grappes indépendantes déjà reliées dans le réseau.

Échelle des sources, de la plus solide à la plus faible : document officiel ou registre, témoin direct identifié, expert qualifié, média établi, relais, anonyme.

## 7. Les contrôles de preuve

Le contrôle de preuve passe d'abord par du code, puis par un modèle :

1. L'URL s'ouvre (code HTTP, sinon copie Wayback).
2. L'extrait est retrouvé mot pour mot dans la transcription à la seconde annoncée (à deux secondes près), ou dans le texte à l'écran de l'image annoncée.
3. L'identifiant partagé est bien égal, la date décodée se recalcule, le registre renvoie bien le nom.
4. Seulement ensuite, un modèle dit si l'extrait soutient la phrase.

Un échec retire le lien, rouvre la question et s'inscrit au journal des refus. Un second contrôle de pertinence écarte les doublons et les résumés trop longs ; une pièce écartée reste gardée et cherchable, car un filtre trop dur tue le signal faible.

## 8. L'échelle de conclusion

Deux axes séparés : le *niveau* (ce que l'on a établi) et la *confiance* (faible, moyenne, forte, avec les trous écrits). Le niveau ne monte jamais sans preuve nouvelle.

- **N1 Indice isolé** : « nous observons X ».
- **N2 Coordination probable** : « ces comptes agissent de concert », sans rien dire de l'auteur.
- **N3 Ensemble cohérent** : « même opérateur probable », sous un nom neutre.
- **N4 Lien avec une entité** : « lié à telle société », après validation humaine, avec une preuve de propriété ou de flux et une corroboration indépendante.
- **N5 Attribution** : seulement relayée (« telle autorité attribue à X »), jamais produite par l'outil.

## 9. Les dates et les reprises

Certaines plateformes fabriquent leurs identifiants à partir de l'heure. L'identifiant TikTok contient la date de création, celui d'Instagram aussi. L'outil la décode sans ouvrir la publication (`enquete/temps.py`), puis la compare à la date déclarée par l'index. Chaque vidéo porte deux horloges : déclarée et décodée. YouTube ne permet pas ce calcul.

La plus ancienne occurrence trouvée s'écrit « première trouvée », jamais « première » : l'outil ne voit pas tout.

## 10. Le banc d'essai

Principe détaillé dans [`banc/README.md`](../banc/README.md). Une affaire résolue par la presse fournit un corrigé : les faits à retrouver, ce qui reste non établi, les pièges à éviter. L'outil produit un rendu, chaque fait avec sa preuve, et le programme compare.

- On règle l'outil sur plusieurs affaires, en gardant les écarts de chaque tour pour réécrire une consigne à la fois.
- Une consigne modifiée n'est gardée que si le score monte.
- On s'arrête quand le rappel ne gagne plus un point pendant deux tours de suite.
- Une affaire de réserve est jouée une seule fois, à la fin. Le programme tient un journal et refuse un second passage.

Un chiffre du banc vaut pour les affaires mesurées. Il ne dit rien d'une fiabilité générale.

## 11. Garde-fous

1. **Aucun verdict** : jamais « coupable », « bot », « faux compte », « ingérence ». « Coordination » ne dit ni intention ni identité. Aucune liste de comptes publiée.
2. **Données personnelles** : seul le dossier en cours est conservé. Aucune donnée sensible n'est déduite d'une image.
3. **Mineurs** : jamais nommés, affichés ni proposés comme interlocuteurs.
4. **Ni visage ni voix** : pas de reconnaissance faciale ni vocale.
5. **Personnes privées** : l'agent s'arrête et demande au journaliste. Les pistes par e-mail et par téléphone sont désactivées.
6. **Droit de réponse** : aucune mise en cause n'est exportée sans la case « personne contactée » et la trace des tentatives.
7. **Détecteurs d'IA** : un indice, jamais une phrase « généré par IA » sans provenance.
8. **Sources du journaliste** : une pièce qu'il ajoute (document, note d'appel, récit vécu) n'est jamais envoyée à un service externe. Copie locale et empreinte seulement.
9. **Collecte** : ciblée, sur comptes publics, sans faux compte ni contournement.
10. **Regard** : images violentes floutées par défaut.
11. **Honnêteté sur la portée** : quand l'index est aveugle, l'outil écrit « hors de ma portée » et nomme l'archive, le registre ou la personne qui peut voir. Un chiffre n'est affiché qu'avec la pièce qui le porte.

## 12. La mémoire : le réseau

Le réseau est un fichier SQLite en ajout seul (`enquete/reseau.py`) : le journal refuse toute modification ou suppression, et une correction ajoute une version. Il contient des nœuds typés, des rôles, neuf familles de liens (identité, contenu dit, contenu montré, temps, lieu, infrastructure, argent, relations sociales, récit), les preuves, les explications écartées, les deux notes en mots, les questions ouvertes, et un export pour l'interface. Un lien sans preuve est refusé à la construction.

Chaque pièce archivée à son entrée (`enquete/archive.py`) garde une copie locale, son empreinte SHA-256, la date de collecte et un instantané Wayback. Un dossier peut ainsi être rouvert et vérifié sans l'outil.
