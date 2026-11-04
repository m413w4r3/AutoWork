---
name: autowork-cti-investigation-loop
description: Faire progresser une enquête CTI vers les invariants, les règles YARA/Suricata, le protocole C2 et de nouveaux échantillons. Pour operator_led uniquement.
---

# Boucle de décision CTI

Les observations versionnées font foi; Bridge conseille et peut proposer le texte du rapport, mais ne crée pas de preuve. L’enveloppe, G2/TLP et les outils exposés dans l’enquête courante gardent autorité.

## Collaborer avec Bridge

Charge `autowork-bridge-collaboration` dès le démarrage. Demande un cadrage naturel dès que l’article et l’inventaire initial sont disponibles, puis sollicite Bridge après une découverte importante, pour construire ou améliorer YARA, choisir des pivots VT discriminants, étudier le C2 quand pertinent et rédiger **chaque section analytique** avec `draft_text` (`review_text` si utile). Ces moments sont des déclencheurs, sans quota. Donne le contexte utile dans une phrase, lis le texte brut avant son état de parsing et agis sur chaque recommandation utile. Reviens avec les résultats lorsqu’ils peuvent changer l’analyse; évite une question identique dont la réponse est déjà disponible.

## Boucle souple

1. Choisis une cible CTI utile : données opaques, méthode, message réseau, invariant candidat, règle ou nouvel échantillon.
2. Utilise l’outil autorisé qui peut répondre à cette question. Une inspection simple ne demande ni hypothèse formelle ni liste de contre-tests.
3. Formalise hypothèse, alternative et critère avant le test lorsqu’une question est réellement disputée ou que le contrat l’exige.
4. Lis la couverture et les limites du résultat. Exploite une découverte comme invariant, motif YARA, critère Suricata, élément de protocole ou pivot; persiste un finding ou une expérience lorsque cela apporte une trace utile.
5. Continue la branche productive, reviens vers Bridge pour une critique utile ou passe à une autre piste. Une candidate ou un modèle C2 partiel reste exploitable même sans goodware, holdout, capture ou protocole complet.

Un conseil Bridge mène à un test, une action adaptée ou une raison simple de ne pas le suivre. Une trace en prose dans `purpose` suffit; aucun format de trace ne bloque l’étape suivante.

## Lire les résultats

- `supported` couvre uniquement les faits directement observés; `qualified` limite l’interprétation; `inconclusive` garde la question ouverte.
- `empty` décrit la recherche exécutée, pas une absence globale. `partial`, `unavailable`, `blocked`, `policy_denied` et `needs_review` ne valent pas preuve négative.
- Après un POST ambigu, réconcilie l’invocation existante avant toute reprise; ne renvoie pas le POST. Les autres pistes indépendantes peuvent continuer.

**Oracle :** les observations utiles font avancer détection, protocole ou découverte sans cérémonies inutiles, et chaque piste garde la portée réelle de ses résultats.
