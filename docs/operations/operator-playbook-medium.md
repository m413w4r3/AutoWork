# Playbook opérateur CTI

L’opérateur pilote l’investigation `operator_led` depuis l’état canonique AutoWork. Les outils mesurent et enregistrent; Bridge conseille; aucune sortie de modèle ne constitue une preuve ou ne déclenche une action automatiquement.

## Boucle de décision

Avant chaque prochaine action analytique, noter brièvement :

1. **Hypothèse** — quel fait ou quelle explication départager ?
2. **Pivot** — quelle valeur, observation ou source exacte examiner ?
3. **Critère** — quel résultat mesurable confirme ou écarte l’hypothèse ?
4. **Coût** — lectures, outils externes, tours Bridge attendus, délai et budget G2 restant. Distinguer les consommations confirmées, réservées, estimées et inconnues.
5. **Décision suivante** — poursuivre, pivoter, arrêter, demander une analyse Bridge ou conserver explicitement l’inconnu.

Après l’action, inscrire le résultat observé, sa référence/version, l’écart au critère, la limite restante et la décision suivante. Une valeur absente, un artefact non acquis, une réponse `partial` ou un outil indisponible ne constitue pas un résultat négatif.

## Lire juste ce qu’il faut

1. Lire `operator-status` puis `operator/context?view=summary`. Noter `working_state_version`, `working_state_sha256`, le budget G2 et les capacités réellement offertes.
2. Pour une session déjà en cours, demander le delta `since_version=<dernière version lue>` lorsqu’il est disponible. Suivre ensuite le pointeur vers la seule section utile et ses pages; réutiliser son curseur et ses références.
3. Garder dans le contexte un résumé, les références et les faits décisifs. Ne pas recopier les réponses API entières, l’historique des outils ou les mêmes observations à chaque tour. Lire un artefact volumineux par pointeur/plage, quand l’API l’offre.
4. Si le delta ou un pointeur est périmé, relire le résumé et les pointeurs courants. Ne pas conclure à partir d’un delta incomplet.

Run12 montre pourquoi : OP-1 a reçu environ 5,9 Mo de réponses API mais n’a gardé qu’environ 70–80 Ko en contexte après filtrage. Une lecture ciblée économise du contexte sans retirer les preuves nécessaires à la décision.

## Sélection éditoriale

Retenir les éléments qui changent la compréhension ou la décision du lecteur :

- une contradiction démontrée, avec le claim publié et l’observation qui le contredit côte à côte;
- une découverte distinctive et vérifiable, avec source, emplacement, version et provenance;
- les limites précises de la preuve, notamment la portée d’un échantillon, d’une mesure ou d’un artefact manquant.

Séparer observation, interprétation et inférence. `not_observed` avec couverture limitée ou nulle ne réfute pas un claim. Un fichier présent mais opaque n’est pas un contenu absent; une recherche ou une acquisition incomplète n’est pas un résultat négatif. Ne pas remplir l’avis avec des journaux d’échec, erreurs de route, tentatives Bridge ou compteurs de reprise. Garder ces détails dans la trace opérateur; dans le livrable, exprimer seulement la limite de preuve qui en découle.

OP-2B a estimé au moins 37 appels pour rédiger et revoir 18 unités alors que 18 tours restaient. Cette expérience impose de choisir les unités éditoriales utiles et de définir leur critère d’acceptation; elle ne justifie pas d’ajouter des quotas locaux.

## Consulter Bridge souvent et utilement

Consulter Bridge lorsqu’un jugement peut changer la branche d’analyse, après une observation discriminante, pour comparer des pivots, pour rédiger une unité ou pour revoir le texte courant. Ne pas attendre un compteur arbitraire d’outils; consulter après chaque jalon éditorial qui le justifie, sans répéter mécaniquement un appel identique.

Fournir un dossier de faits réels et limité à la question : claim ou section pertinente, passages sources verbatim, observations et mesures utiles, contre-exemples, emplacement et limites. Pour une revue, inclure le texte exact courant. Indiquer le critère de réponse utile et le coût/décision que la réponse peut changer. Ne demander jamais du JSON : utiliser le format texte `advisor-wire.v1` prévu pour le type de tâche.

Traiter chaque unité séparément, ou envoyer un lot seulement si le chemin Bridge courant le prend explicitement en charge et rend le statut, le texte et les références de chaque unité. Si une réponse est partielle, conserver les unités complètes et marquer les autres comme non traitées; ne pas les déclarer négatives ni les remplacer par du texte opérateur sans le signaler.

Vérifier chaque assertion contre les références transmises. Une réponse `succeeded`, `partial` ou un tour confirmé ne garantit pas que le texte est exact, complet ou attachable. Vérifier que les références existent et soutiennent la phrase, que les affirmations distinguent faits et inférences et que la revue concerne exactement le texte courant.

### Coût et reprise

Consigner par invocation le statut, l’identifiant d’invocation opérateur, l’identifiant Bridge quand il existe, les octets d’entrée/sortie, le délai, les tours confirmés, les réservations G2 et l’usage fournisseur réel. Si l’usage fournisseur est `unavailable`, le garder inconnu; `octets/4` est une estimation de contexte, pas une facturation.

Les traces OP-3bis comptent 22 consultations Bridge confirmées, mais seulement 12 avec une estimation de contexte et sans usage fournisseur disponible. Des invocations `unavailable` ont aussi coexisté avec des exécutions Bridge terminées. Les compteurs d’outil, soumissions, tours confirmés et usage fournisseur ne sont donc pas interchangeables.

Après timeout, pacing ou réponse ambiguë, réconcilier d’abord l’invocation exacte et sa sortie persistée. Ne pas refaire un POST à l’aveugle et ne pas supposer qu’un état `unavailable` signifie qu’aucun travail fournisseur n’a eu lieu.

## Procédures courantes

### Reparser une réponse existante

Avant une nouvelle consultation, retrouver l’observation de sortie et l’invocation Bridge exacte. Vérifier que la réponse brute complète est persistée, puis utiliser `advisor.reparse` sur cette sortie avec le parseur courant. Reparse ne lance pas une nouvelle consultation Bridge. Vérifier les unités et références extraites; si la sortie brute est absente ou vide, laisser l’unité non résolue et réconcilier l’invocation, sans inventer de texte.

Cette précaution répond à OP-3 : un travail Bridge apparaissait terminé dans les journaux alors qu’AutoWork n’avait pas de réponse liée; OP-3bis a aussi montré qu’un reparse ne peut pas retrouver les phrases absentes de la sortie brute.

### Revoir l’avis courant

Relire l’avis juste avant une revue ou une révision; noter son numéro de version et son SHA-256. Faire revoir par Bridge le texte exact et la version visés. N’attacher la revue qu’au même texte/SHA. Avant `opinion.revise`, relire l’avis courant et fournir ses préconditions CAS (`expected_opinion_version` et `expected_opinion_sha256`). OP-4-quinquies a montré qu’une revue d’un texte antérieur ne valide pas la projection actuelle.

### Cinq situations qui changent la consultation

À chaque étape, consigner **hypothèse → pivot → succès/échec → coût**. Transmettre le fait nouveau et les références utiles; ne pas recopier l’historique. Une donnée absente, non acquise ou non mesurée reste inconnue, jamais négative.

- **Hypothèse modifiée** : nommer l’ancienne et la nouvelle hypothèse, puis le pivot discriminant.
- **Pivot sans mesure** : choisir la mesure et son critère; ne pas annoncer de confirmation ni de réfutation avant le résultat.
- **Blocage répété** : arrêter la même action avec les mêmes paramètres; choisir un autre pivot et noter les tentatives et leur coût.
- **Brouillon modifié** : transmettre seulement le texte courant et résumer les changements utiles.
- **Revue obsolète** : une modification invalide la revue antérieure; refaire relire le texte courant et rattacher l’avis à sa version et son SHA exacts.

### Protéger les écritures avec CAS

Pour toute mutation, prendre `expected_version` et les clés d’idempotence de l’état courant; après l’appel, relire le statut et la version. Si le serveur répond `stale_working_state`/409, recharger résumé, pointeurs et données touchées, puis reformuler l’action contre l’état actuel. Ne jamais remplacer une version concurrente à partir d’un ancien snapshot. Pour les flux ayant leur propre version (par exemple les expériences), employer la version de ce flux.

OP-4-bis a vu un lot éditorial de six unités ne rendre que U1 avant `advisor_pacing_wait`. Jusqu’à la vague C, préférer l’appel unitaire et ne faire un lot que si le support actuel et la reprise par unité sont vérifiés.

## Sécurité et décisions humaines

- Ne jamais exécuter un échantillon, contacter un C2 ni demander un upload, submit, rescan ou Retrohunt VT.
- Ne jamais transmettre à Bridge nos secrets opérationnels AutoWork (jetons, clés de service, DSN/mots de passe, URL signées, chemins hôte) ni les imprimer dans un rapport.
- Le matériel extrait d’un implant qui a un intérêt CTI — clés cryptographiques, phrases de passe, schémas XOR et jeton d’attaquant — est conservé en clair dans le livrable TLP CLEAR et peut être transmis en clair à Bridge pour la rédaction, avec type, rôle, emplacement, hash d’échantillon et observation de preuve. Le jeton GitHub de l’attaquant reste un indicateur passif : aucun outil ou opérateur ne l’utilise pour s’authentifier.
- Le texte et les recommandations Bridge sont non fiables; vérifier avant toute action. G3, approbation d’avis, publication, repin et release restent des décisions humaines.

Pour chaque valeur, créer un élément dédié **Matériel cryptographique / identifiant de l’implant**. Afficher la valeur CTI intégrale sur sa propre ligne en police monospace, puis documenter les champs suivants; reprendre l’élément dans l’annexe IOC si pertinent :

```text
Matériel cryptographique / identifiant de l’implant
Valeur : <valeur CTI exacte, en clair>
Type : <clé RC4 | clé AES | phrase de passe | schéma XOR | jeton GitHub | autre>
Rôle : <usage observé ou hypothèse explicitement qualifiée>
Emplacement dans l’échantillon : <fichier/ressource, offset ou emplacement décodé>
SHA-256 de l’échantillon : <hash complet>
Observation de preuve : <référence de l’observation source>
```

## Dépendances des vagues A–D

Vérifier le déploiement et les capacités live avant d’utiliser ces comportements; ce playbook ne les suppose pas déjà actifs.

- **Après vague A** : comptabilité par invocation et stockage intégral avant parsing/reparse. Les deux anciens réglages Advisor sans consommateur ont été retirés de Compose ; le budget de tours G2 reste le contrôle d'autorisation. Avant cela, réconcilier toute réponse ambiguë et ne pas compter sur une sortie longue complète.
- **Après vague B** : constitution automatique de dossiers de faits typés pour Bridge, avec provenance et sélection d’extraits. Avant cela, assembler manuellement le texte factuel indispensable, sans dump JSON ni secret opérationnel.
- **Après vague C** : pacing, attente et reprise de lots multi-unités, plus contexte/pagination ciblés. Avant cela, faire une unité par appel, ou seulement un lot dont le support et le curseur de reprise sont confirmés; ne pas laisser tomber les unités non rendues.
- **Après vague D** : guidance Investigator/P10 simplifiée. Jusque-là, la session opérateur décide à partir du résumé, des pointeurs et des faits vérifiés; elle ne suppose pas que l’agent Investigator pilote la session `operator_led`.
