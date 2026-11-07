# Méthode

Ce document décrit comment l'outil doit raisonner, et ce qu'il s'interdit. Les mécanismes décrits sont généraux : aucune consigne d'agent ne contient de nom, de date ou de fait tiré d'une affaire du banc d'essai.

**État du code.** Ce dépôt contient le dossier sourcé, la collecte, les regroupements, l'archivage et le banc d'essai. Les agents de raisonnement et les vérifications automatiques décrits ci-dessous sont **prévus** quand une phrase le dit ; sans mention, le dépôt le fait. Le tableau complet est dans le [README](../README.md).

## 1. Le point de départ

Le journaliste donne deux choses :

- **une question** (« Stylos minceur : qui est derrière ? », comme on dirait « Panama Papers ») ;
- **une pièce de départ**, facultative : une vidéo envoyée par un abonné, une capture d'écran, un lien, le récit d'une arnaque vécue.

L'outil en tire les éléments à suivre (pseudo, phrase exacte, produit, lien en bio, montant, lieu). Il choisit une ou deux grilles d'enquête, puis formule trois ou quatre hypothèses concurrentes, dont une qui donne tort au journaliste. Pour chacune, il écrit ce qui la ferait tomber. Il cherche d'abord à réfuter l'hypothèse dominante. (Prévu : ce choix et cette rédaction sont faits par des agents absents de ce dépôt.)

Sans pièce de départ, la question seule suffit : la collecte part de ses mots.

## 2. Les grilles d'enquête et les questions ouvertes

Une grille d'enquête est la liste de contrôle d'un enquêteur chevronné : devant une arnaque, il lui faut le vendeur, le chemin de l'argent, les personnes qui se disent lésées, l'autorité qui a déjà agi. Chaque rubrique vide devient une question ouverte numérotée, avec deux ou trois pistes (où chercher, par quelle source, à quel coût). La liste existe donc avant la première recherche.

| Grille | Ce qu'elle oblige à chercher |
|---|---|
| Promesse commerciale | première annonce, qui encaisse, chemin lien en bio → site → mentions légales → société, décision d'autorité |
| Désinformation santé | l'affirmation mot pour mot, qui vend le remède, ce qu'en dit l'agence sanitaire, un expert qui répond déjà |
| Rumeur virale | la première occurrence trouvée, le moment où la rumeur sort de son cercle d'origine, la déformation du récit d'un relais à l'autre, le démenti daté |
| Contenus recyclés | date et lieu de l'original, première reprise, légendes qui changent selon le pays |
| Fabrication (faux propos, montage, IA) | la vraie vidéo d'origine, ce qui a été changé, le démenti |
| Publications synchronisées (à établir) | gestes répétés à la même heure, même texte, moyens techniques partagés |

Les rôles de base sont : plus ancienne trace retrouvée, relais, à qui ça profite, moyens techniques, personnes qui se disent lésées, témoins directs, experts, voix contraires, mis en cause, zones d'ombre. La grille dit lesquels sont attendus. Une grille libre (les dix rôles, sans faits attendus) sert quand rien ne colle. (Dans le code, ces rôles portent encore leurs identifiants d'origine : `origine`, `amplificateur`, `beneficiaire`, `infrastructure`, `victime`, `temoin`, `expert`, `contradicteur`, `mis_en_cause`, `inconnu`.)

**Règles des rubriques.**

- Une rubrique ne se remplit que par une pièce qui a passé la vérification de preuve. Une intuition devient une piste, rangée sous la question.
- Une question porte un texte, un rôle visé, des pistes, et un statut : *ouverte* ou *éclaircie* (par la pièce nommée). Deux statuts de plus sont prévus : *en cours*, et *hors du champ de l'outil* (« il faut un humain : voici qui peut voir »). Le code n'a aujourd'hui que les deux premiers (`StatutQuestion`).
- Une contradiction entre deux pièces ouvre une question (« 550 000 ou 800 000 manifestants ? ») au lieu d'être tranchée.

## 3. Les six questions de l'enquêteur

Posées à chaque affaire, dans cet ordre :

1. **Qu'est-ce qui ne colle pas ?** Une date impossible (la vidéo est plus vieille que l'événement), un compte créé juste avant, la même phrase mot pour mot sur plusieurs comptes, des publications synchronisées à la minute, un lieu ou une langue qui ne correspondent pas à l'image.
2. **D'où ça vient ?** La plus ancienne trace retrouvée, et ce que l'on ne peut pas voir. Les plateformes hors du champ de l'outil sont dites, pas ignorées.
3. **À qui ça profite ?** Quatre gains :
   - *argent* : vente directe, affiliation, code promo, lien en bio vers une boutique, rémunération aux vues, cagnotte, abonnement, contacts revendus ;
   - *audience* : croissance d'un compte, revente de compte, ferme à clics ;
   - *influence* : cause politique ou acteur étranger, jamais conclu sans source solide, jamais sur la seule langue ou le seul fuseau horaire ;
   - *nuisance* : nuire à un concurrent, une personne, une institution.

   Pour chaque gain retenu, il faut dire où passe la valeur, la pièce qui le montre et ce qui manque. Sans pièce, c'est une hypothèse.
4. **Qui fait tourner la machine, et où va l'argent ?** On suit les moyens techniques : lien en bio, domaine (date de création, hébergeur, domaines frères), boutique, code promo, pseudo réutilisé, mentions légales et SIREN, société, canal Telegram. On suit aussi les flux : adresses de portefeuilles crypto visibles dans une bio, une légende, le texte à l'écran ou un QR code ; transactions retrouvées dans le registre public de la blockchain ; portefeuilles qui ont payé ensemble ; liens de paiement et de cagnotte. Un portefeuille de plateforme d'échange, partagé par des milliers d'utilisateurs, ne fait qu'un lien faible. Un portefeuille n'est jamais attribué à une personne sans source solide. (Codé : `enquete/pivots.py` et `enquete/sources/chaines.py`, sauf la lecture des QR codes et du texte à l'écran, prévue.)
5. **Quelle est l'affaire derrière l'affaire ?** L'affaire visible est souvent un symptôme. On monte en généralité de trois façons, chacune posée comme hypothèse avec le test qui la prouverait et celui qui la réfuterait : *le réseau* (les mêmes acteurs dans d'autres affaires), *le système* (le modèle économique qui rend l'affaire rentable), *la faille* (qui aurait dû l'empêcher, et pourquoi cela n'a pas suffi).
6. **Qui doit répondre, qui peut témoigner, qui a démenti ?** Les mis en cause, à contacter avant publication, les témoins directs, les experts, les voix contraires.

## 4. Ce qui fait un bon rapprochement

- **Précis.** Un lien dit une relation précise : reprend, même opérateur, même infrastructure, même argent, coordonné avec, profite à, contredit, recyclé, généré par IA. Jamais « lié à » tout court.
- **Sourcé.** Il porte son adresse, sa citation exacte et sa seconde. Le code refuse un lien qui n'en a pas ; la vérification automatique que la citation figure bien dans la pièce est prévue. Sans preuve, c'est une piste.
- **Pesé selon la rareté du point commun.** Hashtag populaire, son tendance, modèle de montage répandu : ces points communs ne prouvent rien. Même code promo, même domaine, même faute d'orthographe, même SIREN, même plan coupé à la même image : ils pèsent lourd. Plus un point commun est rare dans les vidéos collectées, plus il compte (modèle de Fellegi et Sunter, pour le lecteur technique : poids de l'ordre de −log de la fréquence ; `pivots.poids_rarete`).
- **Recoupé.** Seuls s'additionnent des indices qui ne viennent pas du même geste. Une vidéo republiée compte pour un seul indice. Un indice contraire fait reculer le rapprochement ; une information absente ne compte ni pour ni contre. L'outil affiche la force des indices, jamais une probabilité que le lien soit vrai. (Prévu : le code ne teste pas encore l'indépendance des indices.)
- **Confronté aux autres explications.** Chaque faisceau d'indices a au moins une autre explication, et l'indice qui les départage (voir §5).
- **Suivi de proche en proche.** Compte → lien en bio → boutique → société → autres boutiques → autres comptes. Une chaîne vaut son maillon le plus faible, et on l'affiche.
- **Chiffré quand c'est possible.** Combien de comptes, de vidéos, d'euros, sur combien de jours.

Échelle de lecture d'un rapprochement, du plus faible au plus fort : indices contraires, coïncidence possible, indice isolé, indices concordants, faisceau d'indices, faisceau d'indices recoupé.

## 5. Les six explications à écarter

Devant deux contenus qui se ressemblent, l'outil écrit d'abord les autres explications, avec ce qui départage chacune :

| Explication | Ce qui la départage |
|---|---|
| Coïncidence | la rareté de l'indice dans les vidéos collectées |
| Tendance virale | le nombre de vidéos par jour sur ce motif |
| Même source citée | presse, recherche web, vidéos plus anciennes |
| Copie | le délai entre les deux, l'empreinte d'image |
| Même auteur | identité, lien en bio, moyens techniques |
| Coordination | comparaison avec des heures de publication tirées au hasard |

Le lien écrit nomme la gagnante et les écartées, avec la preuve qui les écarte. Il ajoute les autres explications propres au terrain (agence de créateurs, « défi » de marque). Le modèle de données les porte (`ExplicationEcartee`) ; leur rédaction par les agents est prévue.

## 6. Deux notes séparées par pièce

Chaque pièce reçoit un rôle et deux notes, établies par deux évaluations séparées, la seconde sans voir la première :

- **fiabilité de la source**, de A à F ;
- **crédibilité de l'information**, de 1 à 6.

Elles sont toujours traduites en mots (« source habituellement fiable ; information recoupée par d'autres sources indépendantes »). Règles mécaniques : un registre ou une décision d'autorité vaut A au mieux ; un compte anonyme ne dépasse jamais E ; « recoupée » exige deux sources indépendantes déjà reliées au dossier. (Codé : les deux notes et leur traduction en mots ; prévu : les règles mécaniques et les deux évaluations.)

Échelle des sources, de la plus solide à la plus faible : document officiel ou registre, témoin direct identifié, expert qualifié, média établi, relais, anonyme.

## 7. Les vérifications de preuve (prévu)

Aucun des contrôles ci-dessous n'est codé dans ce dépôt ; seul le refus d'un lien sans adresse ni citation l'est. La cible : la vérification passe d'abord par une vérification automatique, puis par une relecture faite par une IA :

1. La page s'ouvre encore (sinon, copie conservée par Internet Archive).
2. La citation est retrouvée mot pour mot dans la transcription à la seconde annoncée (à deux secondes près), ou dans le texte à l'écran de l'image annoncée.
3. Le numéro partagé est bien égal, la date retrouvée se recalcule, le registre renvoie bien le nom.
4. Seulement ensuite, une IA dit si la citation soutient la phrase.

Un échec retire le lien, rouvre la question et s'inscrit au journal des refus. Un second contrôle de pertinence écarte les doublons et les résumés trop longs ; une pièce écartée reste gardée et cherchable, car un filtre trop dur tue le signal faible.

## 8. L'échelle de conclusion

Deux axes séparés : le *niveau* (ce que l'on a établi) et la *certitude* (cinq mots du lexique de rédaction, avec les trous écrits). Le niveau ne monte jamais sans preuve nouvelle. Les niveaux portent leur nom, jamais un code.

- **Indice isolé** : « nous observons X ».
- **Synchronisation constatée** : « ces comptes ont publié la même chose à quelques minutes d'écart », sans rien dire de l'auteur.
- **Mêmes moyens techniques constatés** : « ces comptes partagent le même domaine et la même boutique ».
- **Rattaché à une société, selon un registre ou un document** (« selon le registre du commerce, … »), après validation du journaliste, avec une preuve de propriété ou de flux et une corroboration indépendante.
- **Désignation par une autorité, relayée telle quelle** (« selon … »), jamais produite par l'outil.

(Prévu : cette échelle n'est pas encore codée.)

## 9. Les dates et les reprises

Certaines plateformes fabriquent leurs numéros à partir de l'heure. Le numéro TikTok contient la date de création, celui d'Instagram aussi. L'outil la décode sans ouvrir la publication (`enquete/temps.py`), puis la compare à la date déclarée par l'index. Chaque vidéo a deux dates : celle qu'annonce la plateforme et celle retrouvée à partir de son numéro. YouTube ne permet pas ce calcul.

La plus ancienne occurrence trouvée s'écrit « première trouvée », jamais « première » : l'outil ne voit pas tout.

## 10. Le banc d'essai

Principe détaillé dans [`banc/README.md`](../banc/README.md). Une affaire résolue par la presse fournit une affaire de référence : les faits à retrouver, ce qui reste non établi, les fausses pistes à éviter. L'outil produit un rendu, chaque fait avec sa preuve, et le programme compare.

- On améliore l'outil sur plusieurs affaires en notant ce qu'il a raté à chaque tour, pour réécrire une consigne à la fois.
- Une consigne modifiée n'est gardée que si le résultat monte.
- On s'arrête quand la part de faits retrouvés ne progresse plus d'un point pendant deux tours de suite.
- Une affaire de réserve est jouée une seule fois, à la fin. Le programme tient un journal et refuse un second passage.

Un chiffre du banc vaut pour les affaires mesurées. Il ne dit rien d'une fiabilité générale.

## 11. Garde-fous

Ce sont des engagements de l'outil. Les contrôles automatiques qui les imposent sont prévus (un module de garde-fous n'existe pas encore dans ce dépôt) ; en attendant, c'est la relecture du journaliste qui les tient. Le cadre juridique est dans [`ethique.md`](ethique.md).

1. **Aucun verdict** : jamais « coupable », « bot », « faux compte », « ingérence ». « Coordination » ne dit ni intention ni identité. Aucune liste de comptes publiée.
2. **Données personnelles** : seul le dossier en cours est conservé. Aucune donnée sensible n'est déduite d'une image.
3. **Mineurs** : jamais nommés, affichés ni proposés comme interlocuteurs.
4. **Ni visage ni voix** : pas de reconnaissance faciale ni vocale.
5. **Personnes privées** : l'outil s'arrête et demande au journaliste. L'outil ne remonte ni adresse e-mail ni numéro de téléphone.
6. **Contradictoire** : aucune mise en cause n'est exportée sans la sollicitation consignée (date, canal, réponse ou silence).
7. **Détecteurs d'IA** : un indice, jamais une phrase « généré par IA » sans provenance.
8. **Sources du journaliste** : une pièce qu'il ajoute (document, note d'appel, récit vécu) n'est jamais envoyée à un service externe. Copie locale et empreinte seulement.
9. **Collecte** : ciblée, sur comptes publics, sans faux compte ni contournement.
10. **Regard** : images violentes floutées par défaut.
11. **Honnêteté sur la portée** : quand l'index est aveugle, l'outil écrit « hors du champ de l'outil » et nomme l'archive, le registre ou la personne qui peut voir. Un chiffre n'est affiché qu'avec la pièce qui le porte.

## 12. La mémoire : le dossier

Le dossier est un journal inaltérable (`enquete/reseau.py`, stocké dans un fichier SQLite) : rien ne s'efface, une correction ajoute une version datée. Il contient des pièces classées par nature, des rôles, neuf familles de liens (identité, contenu dit, contenu montré, temps, lieu, infrastructure, argent, relations sociales, récit), les citations, les autres explications envisagées, les deux notes en mots et les questions ouvertes, plus un export pour l'interface. Un lien sans preuve est refusé à la construction.

Chaque pièce archivée à son entrée (`enquete/archive.py`) garde une copie locale, son empreinte SHA-256, la date de collecte et, quand il aboutit, un instantané Wayback. Un dossier peut ainsi être rouvert et vérifié sans l'outil. Limite mesurée : sur le premier passage réel, les copies n'ont pas toutes abouti (mesure du 7 nov. 2026 sur le rapport de passage, non versionné).
