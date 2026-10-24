# Mission 04 / L1 — contre-revue indépendante R2

**Verdict : REVISE**
Base du delta : `895a24fdf0320257374bc899d81ad91fbc782ab5`
HEAD revu : `e0d1fb6d5511048a5584e9ca87cbf22efd4f3166`

## Findings

- **HIGH — bloque L1 : collision avec le marqueur** — [invariant_proposals.py:2368](../../../backend/src/cti_app/application/invariant_proposals.py:2368). La garde `escaped_value.startswith("[REDACTED]")` laisse passer la valeur entière. Sur le seam réel, `{"auth": "[REDACTED]FIXTURE_MARKER_TAIL"}` conserve `FIXTURE_MARKER_TAIL` dans le prompt. Le marqueur peut être présent dans une valeur source ; cette garde ne distingue pas un texte déjà redigé d'un secret qui commence pareil.
- **HIGH — bloque L1 : coût quadratique** — [invariant_proposals.py:2301](../../../backend/src/cti_app/application/invariant_proposals.py:2301). Une longue valeur unique non fermée est traitée approximativement en temps linéaire (250 k caractères : 72 ms). En revanche, une chaîne synthétique répétant des assignments échappés non fermés oblige le moteur à rescanner les suffixes : 12 813 caractères / 800 labels = 751 ms ; 25 613 caractères / 1 600 labels = 2 969 ms, avec un facteur proche de 4 quand la taille double. Le texte vient de claims non fiables et P10 n'a pas de plafond d'octets ; c'est un risque de déni de service à corriger.
- **MEDIUM — bloque L1 : délimiteur final corrompu** — [invariant_proposals.py:2304](../../../backend/src/cti_app/application/invariant_proposals.py:2304). Une valeur JSON valide finissant par un antislash est bien masquée, mais l'antislash du guillemet fermant disparaît aussi. La forme attendue `r'{\"auth\": \"[REDACTED]\"}'` devient `r'{\"auth\": \"[REDACTED]"}'`. La conservation exacte est donc incomplète pour cette valeur.
- **MEDIUM — hors contrat si limité à une seule couche valide** — une forme ambiguë `r'{\"auth\": \"alpha \"}FIXTURE_TAIL\"}'` ferme le champ au premier `\"}` et laisse `FIXTURE_TAIL`. La forme correctement échappée d'un guillemet interne suivi de `}` (séquence interne `\\\"`) a été redigée en entier. Si le matcher doit couvrir du JSON mal formé ou du texte arbitrairement échappé, ce suffixe résiduel bloque aussi ; le périmètre annoncé exclut cette interprétation récursive/non canonique.

## Contrôles adversariaux

- **Passent :** un scalaire suivi de `}`, `]`, d'une clé échappée après virgule, ou de la fin de chaîne est masqué ; LF et CRLF après le scalaire restent intacts. Un guillemet interne correctement échappé suivi de `}` et d'un suffixe est consommé entièrement. Les délimiteurs sont exacts pour les cas ordinaires, les espaces et les guillemets internes couverts par les tests.
- **Régression non observée :** `author=Jane` et `\"author\": \"Jane Doe\"` restent intacts ; `auth=FIXTURE_PLAIN_AUTH` reste redigé ; le CVE, le marqueur CTI adjacent et le paramètre URL bénin restent présents. Le diff ajoute un matcher de texte, sans changement du parcours des champs structurés, de `_SENSITIVE_NAMES`, ni de la politique G2/TLP/DNT/source/échantillon.
- **Couche d'échappement :** une entrée sérialisée deux fois conserve son fixture non redigé ; aucun décodage récursif observé. Ceci respecte le périmètre documenté d'une seule couche.

## Commandes et résultats

Python 3.12, variables Conda/venv retirées ; pytest exécuté sans cache pytest ni bytecode. Préfixe pytest exact : `cd /home/nill/perso/work/AutoWork_m04_l1/backend && env -u CONDA_PREFIX -u CONDA_DEFAULT_ENV -u CONDA_SHLVL -u CONDA_PROMPT_MODIFIER -u _CE_CONDA -u _CE_M -u VIRTUAL_ENV PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' UV_CACHE_DIR=/tmp/m04-l1-uv-noproject-cache uv run --no-project --python 3.12`.

- `pytest tests/test_invariant_proposals.py -q --tb=short` — **64 passed**.
- `pytest tests/test_invariant_proposals.py::test_render_p10_prompt_redacts_complete_escaped_json_scalar tests/test_invariant_proposals.py::test_render_p10_prompt_preserves_escaped_innocent_author_and_plain_assignment -q --tb=short` — **5 passed**.
- Avec le même préfixe, `ruff check src/cti_app/application/invariant_proposals.py tests/test_invariant_proposals.py` — **All checks passed**.
- Avec le même préfixe, `ruff format --check src/cti_app/application/invariant_proposals.py tests/test_invariant_proposals.py` — **2 files already formatted**.
- `git diff --check e120a36f1afa08261fdf0276e0200872973ad8f2..e0d1fb6d5511048a5584e9ca87cbf22efd4f3166` — code propre ; signale uniquement une ligne vide finale dans `M04_L1_NOTES.md:158` (exit 2).

## Condition de nouvelle revue

Remplacer la garde de préfixe par une distinction sûre entre un marqueur déjà produit et une valeur source, supprimer le rescannage quadratique, et préserver le délimiteur pour les valeurs terminées par antislash. Ajouter des tests de non-régression pour ces cas, puis relancer la suite ciblée et les vérifications Ruff. `NEEDS_DECISION_P10_VERSION` reste une décision humaine distincte ; elle ne bloque pas L1 selon l'handoff §2.
