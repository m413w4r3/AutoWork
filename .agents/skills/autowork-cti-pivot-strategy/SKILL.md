---
name: autowork-cti-pivot-strategy
description: Transformer des invariants techniques, YARA, éléments C2 et relations VT en nouveaux échantillons et résultats discriminants sans nombre minimal de pivots ni hypothèses imposées.
---

# Pivot = test, pas collection d'IOC

Un pivot doit réduire une incertitude explicitée. La relation entre deux indicateurs est un fait à qualifier, pas une preuve automatique de parenté malveillante. Deux résultats issus du même fournisseur ne sont pas deux sources indépendantes.

## Pivot utile, en une phrase

Partir d'un indicateur **réellement observé** et dire pourquoi le résultat recherché pourrait apporter une variante, un élément de C2, un nouveau positif YARA ou un contre-exemple. Noter la référence et la qualité de la relation (observée, rapportée, inférée) au niveau qui permet de la retrouver ; il n'y a pas de fiche obligatoire à sept champs pour chaque recherche.

Pour une simple recherche de hash ou de relation passive VT autorisée, ne pas imposer de nouveau hypothesis.declare ni de protocole de falsification. Formaliser une expérience pour une question réellement contestée ou lorsque le contrat/objectif le demande.

## Familles de pivots

- Identité et dérivation : SHA exact, parent archive→membre, nom/chemin dans un rapport, lien de compilation rapporté.
- Structure binaire : fonctions, xrefs, imports distinctifs, ressources, sections et constantes.
- Similarité : comparaisons de fonctions/capacités mesurées ; caractéristique banale vs rare ; revalider la qualification de cluster.
- Infrastructure : domaine, URL, IP, TLS, DNS, temporel et hébergement partagé ; relation passive ≠ C2 actif.
- Comportement observé : captures, sandbox, appels/flux réellement enregistrés et limites de collecte.
- Détection : motifs YARA traçables, positifs qualifiés et goodware ; correspondance ≠ attribution.
- Texte de renseignement : revendication source, contradiction et observation indépendante.

## Stratégie avant la campagne

Avant un pivot VT important ou après une découverte qui change les candidats, demande à Bridge (`pivot_strategy`) quel test exposé serait le plus discriminant. Résume en une phrase l’invariant vérifié, la relation et ce que tu cherches; le dossier est assemblé automatiquement et les références sont facultatives. Choisis les pivots dont le résultat peut modifier l’évaluation du corpus; un seul peut suffire. Un résultat vide décrit la recherche faite et n’arrête pas une piste différente encore utile.

Lis le texte brut de Bridge avant son état de parsing. Agis sur toute recommandation utile avec les outils autorisés, puis envoie-lui les résultats lorsqu’ils permettent une critique ou une réévaluation. Évite une question identique lorsque sa réponse est déjà disponible; aucune validation de forme ne conditionne l’échange suivant.

## Pivots par motif et qualification des candidats

Quand `available_tools` offre une recherche VT par contenu (`vt.intelligence_search`), c'est le pivot naturel après une règle ou un invariant : nom de ressource, constante, chaîne rare, séquence d'octets. Un `new_pivot_discovered` répété dans `advisor_guidance` signale qu'un indicateur attend d'être exploité. Si la recherche par motif est indisponible, dis-le et dis quel pivot de relations la remplace. Chaque candidat rencontré reçoit une phrase de qualification dans ton compte rendu : déjà dans le corpus de l'article, déjà connu de l'enquête, ou réellement nouveau, et ce que la relation démontre (conteneur, composant légitime, variante).

## Boucle découverte ↔ détection

- Choisir **un** pivot prometteur quand il suffit. Comparer plusieurs options seulement s'il y a une vraie hésitation coût/valeur ; aucun minimum de deux ou trois tests.
- Vérifier les capacités et autorisations G2/TLP actuelles ; ne pas introduire un veto local sur VT passif ou Bridge lorsque l'enveloppe les autorise. Les acquitions, soumissions actives et Retrohunt suivent leurs contrats spécifiques.
- Lire les objets renvoyés, conserver provenance et couverture, puis trier les meilleurs candidats. Un hash relié par VT peut justifier une investigation **sans** être déjà un membre confirmé de famille.
- Lorsqu'un nouvel échantillon est associé/acquis, comparer des invariants et tester la YARA existante si possible. Les matches et non-matches mesurables guident la révision ; les octets absents ne sont pas un non-match.
- Un motif YARA ou un champ C2 peut devenir pivot vers de nouveaux objets ; une variante nouvellement trouvée peut améliorer YARA/Suricata et la description du protocole.
- Enregistrer une expérience ou un finding lorsque le résultat le justifie ; ne pas imposer une mutation canonique après chaque lecture passive.

Un candidat qui ne correspond pas peut servir de contre-exemple pour la YARA; ne le promeus pas en positif de famille par défaut. Un match YARA ou une relation VT ne prouve pas à lui seul la parenté. Après un résultat YARA nouveau, demande une critique à Bridge (`yara_draft`) avec une phrase sur les différences et matches mesurés; si sa réponse contient du code, importe-le selon `autowork-yara-bridge-workflow`.

Ne pas fabriquer de corroboration avec une circularité entre rapports. Une relation fournisseur, un import générique ou un imphash ne prouvent pas à eux seuls une chaîne commune ; ils peuvent néanmoins constituer une piste raisonnable.

## Arrêt

Abandonner la branche si le pivot est peu spécifique, son résultat ne change aucune hypothèse, le garde a déjà été observé, les sources se répètent, ou une nouvelle preuve/autorisation est nécessaire. Noter la condition de reprise.

**Oracle :** un invariant spécifique mène à un pivot VT autorisé, au triage d'un nouveau candidat puis éventuellement à un test YARA ; un import banal ne déclenche pas une attribution de famille.
