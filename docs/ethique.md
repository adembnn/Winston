# Éthique, droit et limites

Ce document dit dans quel cadre l'outil traite des informations sur des personnes, ce qu'il s'interdit, et ce qu'il ne voit pas. Il n'est pas un avis juridique : le cadre ci-dessous est à faire relire par le service juridique d'une rédaction avant tout usage réel. L'état du code est précisé pour chaque engagement : **codé**, **en partie** ou **prévu**.

## 1. Base légale

L'outil est fait pour un journaliste qui prépare une publication. Le traitement de données personnelles à cette fin relève de l'exception journalistique :

- article 85 du RGPD : les États membres concilient la protection des données et la liberté d'expression et d'information, y compris à des fins journalistiques ;
- article 80 de la loi Informatique et libertés : dispense de plusieurs obligations (information des personnes, droits d'accès, limites sur les données sensibles) pour les traitements à des fins journalistiques, dans le respect des règles déontologiques de la profession.

Ce que cette exception ne fait pas : elle ne dispense ni de la loi de 1881 (diffamation, injure), ni de la présomption d'innocence, ni du droit à l'image, ni de la sécurité des données. Elle suppose aussi une finalité journalistique réelle : l'outil ne sert pas à constituer un fichier de personnes, ni à une surveillance sans publication en vue.

Le **responsable de traitement** est la rédaction qui utilise l'outil, pas ce dépôt. Ce dépôt est un prototype de hackathon ; il ne conserve que le dossier de l'enquête en cours.

## 2. Mineurs

- Un mineur n'est jamais nommé, affiché, ni proposé comme interlocuteur.
- Une vidéo où un mineur est probable (mention d'un établissement scolaire, d'un âge, d'une classe, voix ou apparence enfantine) est signalée au journaliste, qui décide.
- L'outil ne cherche pas à déterminer l'âge d'une personne à partir de son visage ou de sa voix. Il s'appuie sur ce qui est écrit ou dit.

État : engagement. Le contrôle automatique des mineurs probables est **prévu**. Aucun mineur n'est nommé dans ce dépôt, ni dans les affaires de référence du banc d'essai.

## 3. Particuliers

- Un particulier (qui n'est ni une personnalité publique, ni un acteur économique, ni un média) n'entre dans un rôle de l'enquête qu'après un clic du journaliste.
- Les pseudonymes de particuliers sont masqués par défaut dans les exports : le journaliste voit la pièce, le lecteur de l'export voit « compte de particulier ».
- Un dirigeant de société n'est cité que d'après un registre public, avec la date de lecture. Son identité complète n'est pas affichée par défaut (initiales d'abord).
- Une personne qui se dit lésée est rangée avec son consentement ou sous forme anonyme, jamais identifiée par recoupement.
- Aucune reconnaissance de visage ni de voix. Aucune donnée sensible (santé, opinion, orientation) n'est déduite d'une image ou d'un compte. Une donnée de ce type que la personne a elle-même publiée n'est reprise que si elle sert directement l'enquête.
- L'outil ne remonte ni adresse e-mail ni numéro de téléphone.

État : le masquage des dirigeants en initiales est **codé** dans `enquete/sources/infra.py` ; le masquage des pseudonymes de particuliers, le clic obligatoire et le blocage des données sensibles sont **prévus**.

## 4. Portefeuilles et flux d'argent

- Une adresse de portefeuille est publique, mais elle est pseudonyme : elle peut devenir une donnée personnelle dès qu'on la relie à quelqu'un.
- Un portefeuille n'est donc jamais attribué à une personne sans source solide ; seules les étiquettes publiques des explorateurs sont reprises, avec leur source. (**Codé** dans `enquete/sources/chaines.py`.)
- Un portefeuille de plateforme d'échange est marqué « doit répondre » : seule la plateforme peut identifier un titulaire, sur réquisition. (**Codé**.)
- Une adresse de la liste de sanctions du Trésor américain est signalée comme telle, avec la source. Cela décrit une inscription sur une liste, pas une culpabilité. (**Codé**.)
- Le montant d'un flux est parfois estimé (partage entre plusieurs adresses en entrée) ; l'outil le marque comme approximatif. (**Codé**.)
- Un flux ne prouve pas l'intention : « ces adresses ont payé ensemble » ne dit pas « ces personnes sont complices ».

## 5. Contradictoire

Aucune mise en cause n'est exportée sans la **sollicitation** consignée : à qui, quand, par quel canal, et la réponse ou le silence. L'outil prépare la demande, mais ne l'envoie jamais : c'est le journaliste qui contacte.

- Le rôle « mis en cause » n'accepte une personne ou une société qu'avec une source solide (registre, décision d'autorité, document) et un clic du journaliste.
- Les formulations sont celles du conditionnel et de l'attribution (« selon le registre du commerce, … ») ; les mots « coupable », « arnaqueur », « bot », « faux compte » sont interdits.
- Une société est nommée d'après un registre ; son lien avec un site ou une boutique reste « à établir » tant que seul un indice le porte.

État : engagement. Le blocage à l'export et la liste de mots interdits sont **prévus** (aucun module de garde-fous dans ce dépôt).

## 6. Conservation des preuves

Deux besoins se tirent en sens opposé : garder la preuve (une publication peut disparaître), et ne pas garder plus que nécessaire (données personnelles, sources).

- **Preuve** : à l'entrée d'une pièce, copie locale, empreinte SHA-256, date de collecte en UTC, instantané Wayback s'il aboutit ; l'horodatage par une autorité tierce (RFC 3161) est écrit mais pas appelé par la chaîne (**en partie**). La copie d'une vidéo peut échouer (plateforme qui bloque le téléchargement) : le rapport de passage le dit pièce par pièce. Une copie manquante est un trou à dire, jamais une pièce « prouvée ».
- **Journal** : le dossier est inaltérable (**codé**). Une erreur se corrige par une version datée, pas par effacement ; la correction et l'ancienne version restent lisibles. Conséquence juridique à noter : un droit d'effacement ne peut pas s'appliquer à l'intérieur d'un journal ; il s'exerce en retirant la pièce de l'export et de l'index, et en supprimant le dossier entier à la fin de l'enquête.
- **Durée** : l'outil ne conserve que le dossier de l'enquête en cours. Une durée de conservation datée et une purge sont **prévues** ; aujourd'hui la suppression est manuelle (le dossier de sortie n'est jamais versionné).
- **Pièces du journaliste** (documents reçus, notes d'appel, témoignages) : copie locale et empreinte seulement, jamais envoyées à un service externe. Engagement ; l'étiquette « sensible » qui empêche d'en mettre le texte dans une consigne d'agent est **prévue**. Le secret des sources est protégé par la loi du 29 juillet 1881 (article 2) ; l'outil ne doit rien produire qui permette de remonter à une source.
- **Prestataires** : les textes de pièces publiques sont lus par un modèle de langage (Claude) dans le cadre de l'abonnement de l'équipe. Les conditions de conservation de l'index Oriane (durée, usage des requêtes) ne sont pas connues à ce jour. La démonstration n'utilise que des données publiques.

## 7. Collecte

- Comptes publics seulement ; pas de faux compte, pas de contournement de mur de connexion.
- Les conditions d'utilisation des plateformes interdisent souvent la collecte automatisée. L'outil passe par l'API d'Oriane pour la recherche ; la copie d'une vidéo (yt-dlp) est une étape distincte, à la charge de la rédaction, et peut être refusée par la plateforme. Une copie d'écran de la page reste possible en option.
- Si le premier essai de copie échoue, l’outil de copie réessaie en se faisant passer pour un navigateur Chrome (`--impersonate chrome`, `enquete/archive.py`). C’est une usurpation d’identité technique : la rédaction doit pouvoir la couper, ce réglage est prévu.

## 8. Limites assumées (angles morts de couverture)

Ce que l'outil ne voit pas, et ce qu'il doit écrire à la place d'une certitude.

| Angle mort | Effet sur l'enquête | Ce que fait ou fera l'outil |
|---|---|---|
| Facebook, X, Threads : hors de l'index d'Oriane | l'origine d'une rumeur peut y être ; un résultat « rien avant telle date » est faux ici | écrire « hors du champ de l'outil » et nommer qui peut voir ; voie par la presse et les fact-checks (**prévue**) |
| Fenêtre d'environ trois mois | l'original d'une vidéo recyclée est souvent plus ancien | « première trouvée », jamais « première » (**codé** dans la formulation de la méthode) |
| Comptes sous 5 000 abonnés absents | les premiers relais d'une rumeur sont souvent de petits comptes | à afficher dans une bande « ce que l'index ne voit pas » (**prévu**) |
| Derniers jours incomplets (retard de l'index) | une tendance du jour peut manquer | idem |
| Messageries privées, stories, contenus supprimés | une grande part de la circulation réelle | non couvert, dit |
| Vidéos-réponses (duos, stitchs) | liens de reprise manqués | non couvert, dit |
| Langue et texte à l'écran | les recherches suivent les mots choisis ; une reprise traduite ou sans texte échappe à la recherche | traduction de la phrase clé et recherche par l'image : **prévues** |
| Regroupement par phrase identique | deux vidéos peuvent citer la même phrase sans lien entre elles (phrase banale, même source citée) ; un regroupement n'est pas une source | « N vidéos, M comptes, K séries » tant qu'aucun test d'indépendance n'est codé |
| Affaires de référence du banc : peu nombreuses | un score sur quelques affaires n'est pas une fiabilité générale | le dire à chaque fois qu'un chiffre est montré |
| Données : aperçu limité aux pièces collectées | l'absence d'une pièce ne prouve rien | une zone d'ombre est une question, pas un constat |

Un chiffre n'est dit que s'il est mesuré, avec la commande qui le produit et la date de mesure. La règle vaut pour le README, la méthode et la démonstration.

## 9. Ce que l'outil ne fait pas

- Il ne publie rien et n'envoie aucun message à une personne.
- Il ne conclut pas à la culpabilité, à l'intention ni à l'identité réelle derrière un compte.
- Il ne remplace ni la vérification d'un journaliste, ni la relecture d'un juriste.
