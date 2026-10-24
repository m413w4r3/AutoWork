# Mission 04 / L1 — revue indépendante

**Verdict : REVISE**
Base : `e120a36f1afa08261fdf0276e0200872973ad8f2`
Lot : `895a24fdf0320257374bc899d81ad91fbc782ab5`

## Findings

- **HIGH — bloque L1** — [invariant_proposals.py:2303](../../../backend/src/cti_app/application/invariant_proposals.py:2303). La classe de valeur s'arrête aux espaces et guillemets. Sur le seam réel `_normalize_context` → `_render_p10_prompt`, l'entrée synthétique `{"auth": "FIXTURE_WITH SPACE"}` devient `{"auth": "[REDACTED] SPACE"}` ; une valeur avec guillemet interne laisse aussi son suffixe visible. Le secret reste donc partiellement dans le prompt. Le test doit couvrir ces valeurs et le matcher doit consommer le scalaire entier.
- **MEDIUM — bloque L1** — [invariant_proposals.py:2303](../../../backend/src/cti_app/application/invariant_proposals.py:2303). Pour `r'{\"auth\": \"FIXTURE_ESCAPED_AUTH\"}'`, le `\` du délimiteur de fermeture est absorbé par la valeur puis supprimé. Le résultat est `r'{\"auth\": \"[REDACTED]"}'`, avec le guillemet final non échappé. Le test ne vérifie que l'absence de la valeur, pas la conservation exacte des délimiteurs échappés.
- **MEDIUM — résiduel documenté, hors portée étroite** — le passage sur un texte échappé deux fois ne correspond pas au matcher et laisse le fixture intact. Les notes excluent le décodage des couches de sérialisation multiples ; ce comportement n'est pas un bloqueur si L1 reste limité à une couche.
- **MEDIUM — risque de faux positif documenté** — les labels sensibles et `_sensitive_name` n'ont pas changé, mais des valeurs CTI sous `auth`/`api_key` dans cette représentation échappée sont maintenant rédigées. Le test prouve la présence du marqueur CTI, du CVE, de `author=Jane` et du paramètre URL bénin ; il ne prouve pas la conservation d'une valeur portée par un champ CTI lui-même sensible. Ce cas dépasse les preuves adjacentes explicitement testées et demande une décision de politique séparée.

## Vérifications

- Le test parcourt bien `_normalize_context` puis `_render_p10_prompt`, sans mock du seam. Les assertions positives vérifient la présence du CVE, du marqueur, de l'auteur innocent et de `response-content-type`; les valeurs d'identifiants/signatures URL sont aussi vérifiées comme rédigées.
- Le matcher de base, lu via `git show e120a36f:...`, laisse la valeur du fixture échappé intacte. Reproduction synthétique en `/tmp` : ancien matcher → valeur présente ; matcher du lot → fixture simple rédigé. La reproduction en `/tmp` du seam réel révèle les suffixes et le délimiteur ci-dessus.
- Le diff de code touche uniquement la regex d'affectation ; l'ensemble des labels et la politique G2/TLP/DNT/source/échantillon n'ont pas changé. Aucun test existant n'est supprimé ou affaibli. Le diff du lot comprend le code, le test et les notes.
- `P10_PROMPT_VERSION` reste à `"4"`. `make_proposal_turn_idempotency_key` inclut la version dans la clé ; `propose()` consulte une réponse réussie par cette clé avant de créer un nouveau tour. Bumper la version change donc l'identité d'idempotence et peut créer un nouveau tour. `NEEDS_DECISION_P10_VERSION` est motivé ; le handoff §2 précise toutefois que cette décision ne bloque pas le lot L1.

## Commandes et résultats

Python 3.12, variables Conda retirées. Pour les commandes pytest ci-dessous, le préfixe exact était `env -u CONDA_PREFIX -u CONDA_DEFAULT_ENV -u CONDA_SHLVL -u CONDA_PROMPT_MODIFIER -u _CE_CONDA -u _CE_M -u VIRTUAL_ENV PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' UV_CACHE_DIR=/tmp/m04-l1-uv-noproject-cache`. Ruff utilisait le même préfixe sans `PYTHONDONTWRITEBYTECODE` et `PYTEST_ADDOPTS`. Les commandes uv avec environnement projet isolé ont d'abord échoué avant le lancement des tests : cache hors ligne sans `numpy==2.5.4`, puis cache uv par défaut non inscriptible. Les mêmes contrôles ont réussi avec `--no-project`, sans modifier le dépôt :

- `uv run --no-project --python 3.12 pytest tests/test_invariant_proposals.py -q --tb=short` — **59 passed**.
- `uv run --no-project --python 3.12 pytest tests/test_invariant_proposals.py::test_render_p10_prompt_redacts_escaped_json_credential_and_preserves_cti_evidence -q --tb=short` — **1 passed**.
- `uv run --no-project --python 3.12 ruff check src/cti_app/application/invariant_proposals.py tests/test_invariant_proposals.py` — **All checks passed**.
- `uv run --no-project --python 3.12 ruff format --check src/cti_app/application/invariant_proposals.py tests/test_invariant_proposals.py` — **2 files already formatted**.
- `git diff --check e120a36f1afa08261fdf0276e0200872973ad8f2..895a24fdf0320257374bc899d81ad91fbc782ab5` — signale des espaces terminaux utilisés pour les retours Markdown et une ligne vide finale dans `M04_L1_NOTES.md`.

## Condition de révision

Corriger la consommation du scalaire échappé, préserver le délimiteur final et ajouter des assertions pour les valeurs avec espaces/guillemets ainsi que la forme rédigée exacte. Relancer le test ciblé, la suite du fichier et Ruff. La décision de version P10 reste à enregistrer séparément selon l'handoff.
