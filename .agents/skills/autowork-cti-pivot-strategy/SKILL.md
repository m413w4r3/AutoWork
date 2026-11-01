---
name: autowork-cti-pivot-strategy
description: Évaluer, exécuter et arrêter des pivots CTI fondés sur la preuve (hash, code, infrastructure, similarité, réseau, YARA). Charger lors du choix de la prochaine piste en investigation.
---

# Pivot = test, pas collection d'IOC

Un pivot doit réduire une incertitude explicitée. La relation entre deux indicateurs est un fait à qualifier, pas une preuve automatique de parenté malveillante. Deux résultats issus du même fournisseur ne sont pas deux sources indépendantes.

## Carte du pivot

Écrire : (1) hypothèse et contre-hypothèse ; (2) objet/source observation ID+SHA ; (3) type d'arête observée, rapportée ou inférée ; (4) nouvel objet ; (5) résultat qui changerait le verdict ; (6) risque et coût ; (7) couverture et faux positifs possibles.

## Familles de pivots

- Identité et dérivation : SHA exact, parent archive→membre, nom/chemin dans un rapport, lien de compilation rapporté.
- Structure binaire : fonctions, xrefs, imports distinctifs, ressources, sections et constantes.
- Similarité : comparaisons de fonctions/capacités mesurées ; caractéristique banale vs rare ; revalider la qualification de cluster.
- Infrastructure : domaine, URL, IP, TLS, DNS, temporel et hébergement partagé ; relation passive ≠ C2 actif.
- Comportement observé : captures, sandbox, appels/flux réellement enregistrés et limites de collecte.
- Détection : motifs YARA traçables, positifs qualifiés et goodware ; correspondance ≠ attribution.
- Texte de renseignement : revendication source, contradiction et observation indépendante.

## Sélection et exécution

Comparer au moins deux ou trois classes de tests, au besoin une analyse locale au lieu d'un pivot fournisseur. Écarter les recherches déjà couvertes, non discriminantes, trop génériques ou privées de provenance. Une nouvelle observation est utile uniquement si elle modifie une qualification, une décision de collecte ou le choix du test suivant.

Pour un pivot retenu : contrôler les conditions de l'outil et G2/TLP ; noter les prédictions avant l'appel ; exécuter une fois ; lire l'observation versionnée ; rechercher contre-indices et dépendance à la même source. Enregistrer une expérience puis réviser la carte de pivots.

Ne pas construire un cercle de preuve : A mentionne B, B cite A et cette répétition serait traitée comme corroboration. L'association VT ou un imphash partagé ne suffisent pas à déclarer deux samples d'une même campagne. Les résultats empty/partial ne réfutent pas le monde non observé.

## Arrêt

Abandonner la branche si le pivot est peu spécifique, son résultat ne change aucune hypothèse, le garde a déjà été observé, les sources se répètent, ou une nouvelle preuve/autorisation est nécessaire. Noter la condition de reprise.

**Oracle :** deux binaires partageant mscoree.dll!_CorDllMain incitent à une comparaison structurelle ou un lien direct, et non à une attribution automatique à la même chaîne d'infection.