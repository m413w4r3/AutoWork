# Mission 01 F6 — revue critique du lot dédié allowlist

**Objet :** commit `83aa615e80e9071264ebb7ca8ab12fbcd9ad6a8a` (branche
`feat/mission01-f6-evidence-allowlist`), base `78b2b1d9`.
**Revue :** agent Paseo `2e22ca1b-6b7b-4dcd-ae08-37135e918708`, GPT-6 Luna
xhigh, lecture seule, worktree `m01-f6-review`.
**Oracle rejoué :** 162 passed. Données synthétiques, transport factice.

## Verdict : FAIL — lot arrêté BLOCKED après 3 tentatives

Le lot dédié a remplacé l'heuristique par une politique de champs, une
normalisation percent récursive, un budget cumulé après décodage et un
sanitizer de labels. Les trois contournements initiaux (titre data URL,
percent simple/double, deux fragments de 300 octets) sont couverts par les
tests. Mais la revue indépendante a trouvé de nouveaux chemins non couverts.
Conformément à la règle du lot (maximum 3 tentatives, sinon BLOCKED avec
preuve, sans nouveau patch à l'aveugle), F6 n'est **pas** réparé une
quatrième fois et la branche n'est **pas** intégrée.

## HIGH — chemin réseau hors allowlist

Une observation synthétique `protocol_capture` contenant
`dns: [{"raw": base64(600 octets)}]` passe par `_measured_protocol_facts`
(`investigation_advisor_tools.py:2432`), qui sérialise toute la ligne DNS,
`raw` compris ; le dossier la reprend pour `suricata_draft`
(`advisor_dossier.py:1017`). Résultat observé : **1 appel au transport
factice** avec le Base64 complet dans le prompt.

## HIGH — Base64 générique avec espaces

`_base64_decoded_length` (`advisor_dossier.py:74`) retourne `None` si une
valeur non déclarée Base64 contient un espace ; l'inspection la traite
ensuite comme du texte (`advisor_dossier.py:129`). Une valeur de 600 octets
encodés, coupée par un espace, LF ou tabulation, atteint le transport
factice dans chaque cas. La variante espace est retrouvée dans le prompt
après normalisation des espaces.

## HIGH — budget 512 non cumulatif dans tous les cas

Deux data URLs Base64 de 300 octets dans deux `strings[].value` passent
ensemble : l'inspecteur retourne leur taille décodée sans fragments à
agréger (`advisor_dossier.py:127`). Deux observations contenant chacune
300 octets Base64 passent aussi : le cumul est vérifié par observation,
tandis que le contrôle global porte sur les octets rendus du dossier
(`advisor_dossier.py:716`, `1180`). Dans les deux scénarios, le transport
factice reçoit les deux valeurs.

## MEDIUM — omission d'un champ inconnu seul non exposée

Une observation ne contenant que `raw: base64(600 octets)` est bien absente
et n'appelle pas le transport, mais le résultat se limite à
`advisor_preflight_needs_retrieval`, sans raison ni compte d'omission
(`advisor_dossier.py:266`, `791`). Le test direct ne vérifie que
l'extracteur (`test_advisor_completeness.py:545`).

## MEDIUM — data URL non Base64 décodée avant contrôle de taille

`unquote_to_bytes` matérialise le payload avant la comparaison à 512 octets
(`advisor_dossier.py:122`). Un oracle synthétique de 10 000 octets confirme
l'allocation avant refus ; avec une limite de lecture par défaut de
512 Mio (`config.py:111`), la pression mémoire proportionnelle à l'entrée
est un risque plausible, d'impact production **UNKNOWN**.

## Contrôles positifs vérifiés

Refus de `%2F`/`+`, Base64 URL-safe avec/sans padding, neuf couches
percent, séparateurs dans une data URL. Un nom court légitime, du hex court
et le MD5 décodé restent acceptés. Les gates de politique, de rédaction des
secrets et de transport ne sont pas modifiées ; `external_llm_allowed`
précède toujours la préparation du dossier
(`investigation_advisor_tools.py:329`).

## Résiduels et suite

- Réception réelle et présence de ces contenus dans des observations
  canoniques : **UNKNOWN**. Aucun Bridge/fournisseur/donnée runtime.
- Coût : **UNKNOWN**.
- **Décision :** F6 reste `BLOCKED/PARTIAL` avec preuve. Une reprise future
  doit traiter structurellement les chemins réseau distincts
  (`protocol_capture`/`dns.raw`), normaliser les séparateurs avant
  classification Base64, appliquer un budget décodé cumulatif par
  observation **et** global avant tout rendu, refuser la matérialisation
  avant contrôle de taille, et exposer les omissions même sans extrait.
  Aucun patch ne doit être appliqué sans revue indépendante de la nouvelle
  conception.
