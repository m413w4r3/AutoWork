---
name: autowork-yara-bridge-workflow
description: Produire tôt une YARA candidate à partir d'invariants observés, la tester avec le corpus disponible, l'améliorer via Bridge et les nouveaux échantillons sans exiger un holdout préalable.
---

# YARA + Bridge : concevoir, éprouver, qualifier

**Principe :** écrire **tôt** une YARA candidate utile, même sur un seul spécimen, puis élargir son évaluation au fil des pivots. Bridge conseille, le workbench compile et teste, les observations déterminent le niveau de confiance. Un match initial ne vaut pas validation de famille, mais constitue déjà un résultat exploitable.

## 1. Préparation légère

- Identifier l'objectif : détecter un spécimen, un sous-groupe ou une famille présumée. Choisir les motifs déjà observés et leur provenance, puis vérifier que les outils de proposition/test annoncés sont utilisables.
- Pour Bridge, respecter G2/TLP, external_llm_allowed et les droits **actuels** ; ne pas ajouter d'autorisation locale spécifique, de plafond de message ni de quota arbitraire.
- **Aucun minimum de positifs, de goodware ou de holdout pour écrire une candidate.** Si un corpus ou un moteur manque, préparer la proposition et dire ce qui n'a pas pu être compilé/testé ; ne pas baptiser « tested » une candidate non testée. **Ne jamais différer une consultation Bridge uniquement parce que le dossier n'a pas encore de mesure typée : demander une critique ou stratégie à partir des preuves déjà vérifiées ; la canonisation de règle est une étape distincte.**
- Ne pas exiger un finding supported ou un plan d'expérience complet avant de commencer la rédaction.

## 2. Sélectionner des motifs

Extraire des observations mesurées : octets invariants d'une ressource, chaînes spécifiques, structures rares, associations de caractéristiques stables, signatures candidates de features.common / signatures.candidates.

Conserver pour les motifs retenus leur source (sample, observation et région/feature si disponible) et leur raison de discrimination. Éviter les motifs triviaux ou non mesurés. Un motif propre à un seul échantillon peut néanmoins servir à une **règle de spécimen**, explicitement bornée, au lieu d'être interdit.

Comparer plusieurs échantillons **si disponibles et utiles**, et élargir ensuite par pivot VT ou similarité. Ne jamais inventer de mesures ou de références pour satisfaire un validateur.

## 3. Consultation Bridge : critique et production, sans rituel

Utiliser `advisor.consult` avec `yara_draft` dès qu'une critique des marqueurs, de la condition ou des faux positifs possibles peut améliorer une première candidate : inutile d'attendre goodware et holdout. Le dossier ciblé doit rendre disponibles **octets/strings discriminants et offsets**, type/structure du fichier (PE/CLI, natif, etc.), décodages utiles, contexte de fonction, règle actuelle et mesures par échantillon, contre-exemples, article/passage pertinent et observations canoniques. Vérifier leur présence dans le dossier effectivement transmis ; demander une lecture ciblée si une donnée clé manque. **Ne pas envoyer le binaire entier par réflexe**, mais ne pas écarter un extrait nécessaire. Formuler l'objectif spécimen/sous-groupe/famille et une question concrète. Il est normalement plus simple d'omettre `measurements` pour laisser AutoWork sélectionner les mesures vérifiées ; ne fournir les champs exacts que pour forcer une sélection. Des consultations de `pivot_strategy` peuvent aussi aider à rechercher de nouveaux échantillons. Aucune obligation de multiplier les tours.

Demander à Bridge de critiquer spécificité, fragilité, faux positifs, conditions et propositions de règle **en prose / code YARA selon le contrat**, jamais de répondre en JSON ou de mettre la réponse dans un bloc JSON. Un contrat Bridge ou parseur peut produire des résultats partiels ; ne pas considérer un statut succeeded comme validation sémantique.

Le Bridge peut produire une règle réellement utile : `rule.propose_bridge` conserve sa source exacte persistée comme candidate attribuée, sans exiger un lien de mesure pour chaque string ; les strings non mesurées gardent un `justification.status=gap`. En cas de `measurements_invalid`, regarder `measurement_errors` (forme acceptée : `content_pattern_id` OU quartet `blob_id, kind, offset, value`). Ne répéter aucune soumission ambiguë ; si le texte a été persisté mais mal parsé, envisager `advisor.reparse` sans nouvel envoi.

## 3 bis. Boucle de révision

Bridge → candidate → compilation → test → lecture des résultats par hash → retour à Bridge avec ces résultats → nouvelle version. Transmettre à chaque retour ce qui a été **mesuré** (matches, non-matches, non mesurables, contre-exemples), pas une impression. Sur faux négatif, chercher les motifs absents, la condition et l'appartenance réelle du sample au groupe ; sur faux positif, la sous-expression trop générique et l'origine du contre-exemple. Ne pas modifier sans mesure ; consigner dans le `purpose` suivant la disposition du conseil (voir `autowork-bridge-collaboration`).

## 4. Enregistrer et tester

- Voie Bridge : rule.propose_bridge exige les champs réels bridge_observation_id, bridge_invocation_id et rule_source_sha256 du **rule_source complet persisté** ; ne pas les inventer à partir d'un résumé. Vérifier la version du schéma avant l'appel.
- Voie mesurée locale : rule.propose_from_candidates seulement si les candidats et leurs provenances P09/L2 sont réellement disponibles ; sinon rule.propose avec motifs vérifiés et provenance compatible avec son schéma.
- rule.operator_syntax_fix accepte `revision_mode="full_source"` pour une source corrigée par l'opérateur depuis une candidate Bridge : la v2 est attribuée à l'opérateur et dérivée de la v1 Bridge, qui reste inchangée.
- Les résultats portent un diagnostic structuré `{stage, status, reason_code}` et un lifecycle dérivé (`candidate`, `compiled`, `tested`, `validated`). Après proposition ou révision, l'étape suivante est `rule.test` sur la candidate ; la compilation seule ne la teste pas.
- Lire les mesures réellement présentes par hash et rôle (positif, candidat, goodware, holdout si disponibles). Distinguer matches, non_matches, non_measurable et erreurs ; les absences de bytes ne sont pas des non-matches.
- Un match goodware mesuré remet en cause la spécificité ; un unique match initial produit une règle expérimentale si tel est le statut du workbench. Le holdout n'est pas un préalable à la première candidate ; s'il sert à l'optimisation, il cesse d'être indépendant.

## 5. Réviser avec un résultat mesuré

Sur faux négatif réellement mesuré, chercher si le motif est non invariant, la condition trop stricte ou l'échantillon hors cluster. Sur faux positif, identifier la sous-expression banale. Réviser selon ce qui apporte de la valeur, via parent_version courant ; tester/comparer les versions quand les octets et capacités existent. Un nouvel échantillon peut devenir un nouveau positif, un contre-exemple ou un nouveau pivot.

Ne pas optimiser au hasard jusqu'à faire matcher l'échantillon test. Le status candidate/experimental/tested/rejected reste défini par le workbench et la couverture effective, pas par l'intention de l'agent.

## Sortie et arrêt

- Objectif, périmètre et motivation des motifs.
- SHA/version de source YARA et références des mesures.
- Bridge invocation et observation si utilisées ; conseil accepté/rejeté avec raison.
- Résultats réellement disponibles : corpus exact par rôle, moteur s'il a exécuté, matches/non_matches/non_measurable et limites ; ne pas inventer les tests absents.
- Détection candidate, statut de validation réel, risques de faux positifs et condition de reprise.
- Rédaction : faire expliquer la règle par Bridge (`draft_text` : motifs retenus, caractéristiques représentées, condition, échantillons testés, couverture, limites) puis vérifier le texte contre les tests ; la source YARA exacte reste un fichier d'annexe distinct, identifié par son SHA-256.
- Approbation/publication : exclusivement humains, hors de ce skill.

**Oracle :** dès qu'un invariant discriminant est mesuré, l'agent produit une candidate YARA et la teste si possible, puis utilise ses résultats pour découvrir de nouveaux échantillons. Il ne confond pas candidate/experimental/tested, et l'absence de goodware/holdout ne bloque pas la première version.
