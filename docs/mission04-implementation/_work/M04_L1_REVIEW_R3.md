# Mission 04 / L1 — contre-revue R3

**Verdict : REVISE.** Deux cas adversariaux restent ouverts : une fermeture prématurée révèle la fin d’un scalaire credential, et un suffixe mal formé efface une preuve CTI adjacente.

**Base :** e0d1fb6d5511048a5584e9ca87cbf22efd4f3166
**HEAD revu :** 0f5371e06b22a35c17d4d4677be124967a601186

## Findings

### HIGH — fuite après un guillemet interne suivi d’un terminateur

backend/src/cti_app/application/invariant_proposals.py:2436 traite toute séquence avec un nombre impair d’antislashs comme candidate à la fermeture ; :2438 accepte ensuite } ou ] comme frontière et :2441 ferme le scalaire.

Reproduction synthétique valide à une couche d’échappement :

    inner = json.dumps({"auth": 'FIXTURE_PREFIX "} CTI_AFTER_INNER_QUOTE'})
    claim = json.dumps(inner)[1:-1]

Le décodage de claim redonne bien inner, dont la valeur auth contient le suffixe. Dans le rendu _normalize_context → _render_p10_prompt, [REDACTED] est émis puis CTI_AFTER_INNER_QUOTE reste visible. Le même résultat est reproduit avec un guillemet interne suivi de ]. Ces suffixes font partie du scalaire sensible et doivent être masqués. Le test R3 du guillemet final avec antislash ne couvre pas cette fermeture prématurée. **Bloquant pour L1.**

### MEDIUM — perte de preuve CTI sur une clé adjacente mal formée

backend/src/cti_app/application/invariant_proposals.py:2418 renvoie l’état ambigu si la clé après virgule n’a pas de : ; :2443 convertit cette ambiguïté en absence de fermeture ; :2483 remplace alors le reste de la chaîne par le seul marqueur [REDACTED].

Entrée synthétique : prefix {\"auth\":\"FIXTURE_AUTH\", \"evidence\" CVE-2025-31337: observed persistence. Le rendu contient prefix {\"auth\":\"[REDACTED] et perd le CVE ainsi que le constat. C’est borné à la chaîne de claim et la donnée canonique n’est pas modifiée, mais le scénario est réaliste pour un extrait tronqué ou recopié et efface silencieusement une preuve autorisée. L’objectif L1 de docs/mission03-architecture/IMPLEMENTATION_PLAN.md §3 demande explicitement de préserver les preuves CTI adjacentes ; la note résiduelle de M04_L1_NOTES.md:250 documente ce compromis sans satisfaire ce contrat. **Bloquant pour le périmètre L1 déclaré.**

## Vérifications adversariales et non-régression

- Parités de 0, 1, 2 et 3 antislashs littéraux en fin de valeur : les quatre sorties correspondent exactement à r'{\"auth\": \"[REDACTED]\"}'. Les fermetures objet, tableau, clé suivante, fin de chaîne, LF et CRLF sont conservées dans les cas usuels.
- Le contenu exact [REDACTED] reste intact, tandis que [REDACTED]FIXTURE_MARKER_TAIL est entièrement masqué. Le marqueur exact suivi d’un champ adjacent est également conservé par le matcher.
- Le guillemet interne suivi de } et de ] révèle le suffixe, comme décrit dans le finding HIGH.
- Le contrôle négatif du suffixe mal formé supprime le CVE adjacent, comme décrit dans le finding MEDIUM.
- Le champ author innocent, le matcher ordinaire auth=FIXTURE_PLAIN_AUTH, les preuves CVE/CTI adjacentes aux valeurs bien formées et le paramètre bénin d’URL signée restent couverts par les tests existants. Le double échappement reste non décodé récursivement.
- Le diff R3 ne change que le scanner échappé et les tests associés. _SENSITIVE_NAMES, _SENSITIVE_SUFFIXES, _sensitive_name, le matcher d’assignation ordinaire et les politiques G2/TLP/DNT/source/échantillon ne changent pas. Aucun champ auth n’est masqué indistinctement. Aucun test existant n’est supprimé ou affaibli et le test du seam passe par normalisation puis rendu.
- Aucun plafond de taille de prompt n’a été ajouté en production. La garde de 1 seconde est uniquement dans backend/tests/test_invariant_proposals.py:1261. Le harnais synthétique /tmp/m04_l1_r3_adversarial.py, mesurant normalisation → rendu, obtient les médianes de cinq essais suivantes : 1 600 labels / 25 613 caractères = **0,003644 s** ; 3 200 / 51 226 = **0,007021 s**. Le doublement reste proche de 2×, sans rescan quadratique observé. La garde de test est environ 275× la première médiane ; elle est ample dans cet environnement, sans garantie contre une suspension extrême de l’hôte.

## Version P10

P10_PROMPT_VERSION reste "4" (invariant_proposals.py:67) ; aucun bump silencieux n’a eu lieu. make_proposal_turn_idempotency_key incorpore la version et les références immuables (invariant_proposals.py:921), mais pas le prompt rendu : un bump change donc l’identité/idempotence du tour. NEEDS_DECISION_P10_VERSION demeure une décision humaine distincte selon le handoff ; elle n’est pas la cause du verdict REVISE et ne requiert pas de bump pour corriger les deux findings.

## Commandes et résultats

Répertoire des commandes Python : backend/. Préfixe d’environnement utilisé pour pytest/Ruff : Conda et venv retirés, caches pytest/bytecode désactivés, cache UV dans /tmp/m04-l1-uv-noproject-cache, uv run --no-project --python 3.12.

- pytest tests/test_invariant_proposals.py -q --tb=short — **67 passed in 0.57s**.
- Groupe ciblé test_render_p10_prompt_redacts_complete_escaped_json_scalar, test_render_p10_prompt_preserves_escaped_innocent_author_and_plain_assignment, test_render_p10_prompt_redacts_marker_prefix_collision_only_when_not_exact, test_render_p10_prompt_preserves_closing_delimiter_after_trailing_backslash, test_render_p10_prompt_scans_many_unclosed_escaped_assignments_linearly — **8 passed in 0.33s**.
- ruff check src/cti_app/application/invariant_proposals.py tests/test_invariant_proposals.py — **All checks passed**.
- ruff format --check src/cti_app/application/invariant_proposals.py tests/test_invariant_proposals.py — **2 files already formatted**.
- git diff --check e120a36f..0f5371e06b22a35c17d4d4677be124967a601186 — **exit 0**, aucune sortie.

Les commandes pytest/Ruff employaient ce préfixe exact :

    env -u CONDA_PREFIX -u CONDA_DEFAULT_ENV -u CONDA_SHLVL -u CONDA_PROMPT_MODIFIER -u _CE_CONDA -u _CE_M -u VIRTUAL_ENV PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' UV_CACHE_DIR=/tmp/m04-l1-uv-noproject-cache PATH=/home/nill/.local/bin:/usr/local/bin:/usr/bin:/bin uv run --no-project --python 3.12

Le harnais synthétique /tmp/m04_l1_r3_adversarial.py reproduit les deux findings ci-dessus ; ses contrôles de parité, délimiteurs usuels, marqueur, matcher ordinaire et absence de décodage récursif passent.

## Condition de révision

Corriger et couvrir au seam le guillemet interne suivi d’un terminateur sans exposer de suffixe sensible ; préserver les preuves CTI adjacentes au chemin fail-closed, ou rendre toute omission explicite et bornée conformément au contrat L1. Relancer la suite ciblée et les contrôles Ruff après correction.
