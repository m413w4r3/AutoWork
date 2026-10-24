# Mission 04 / L1 — revue finale indépendante R4

**Verdict : REVISE.** HEAD examiné : `b9115d0ec2bbd8d78cbd5aed8ad84144382b4a7b`, delta depuis `0f5371e06b22a35c17d4d4677be124967a601186`. Les deux régressions R3 demandées sont corrigées dans les cas reproduits. Un défaut MEDIUM subsiste : un claim mal formé sans terminateur reconnu perd sa preuve CTI adjacente jusqu’à EOF.

## Standards

- **MEDIUM — omission de preuve sans raison observable** — `backend/src/cti_app/application/invariant_proposals.py:2570-2575`. Pour `prefix {\"auth\":\"FIXTURE_AUTH\nCVE-2025-31337: observed persistence`, le repli remplace la valeur et le reste du claim par `[REDACTED]`; la CVE et l’observation disparaissent. Le même résultat survient avec CRLF. La note de risque existe dans `_work/M04_L1_NOTES.md:346-349`, mais le rendu n’expose pas pourquoi la fin du claim a été omise. Cela contredit `backend/AGENTS.md:57-70` : preuve CTI permissive et raison observable pour chaque omission.

## Spec

- **MEDIUM — préservation CTI adjacente incomplète** — `docs/mission03-architecture/IMPLEMENTATION_PLAN.md:48-49,62-66`. L1 exige de masquer le credential sans effacer les preuves adjacentes. Le cas newline ci-dessus est un suffixe plausible après un extrait JSON tronqué; une newline brute ne peut pas être dans une chaîne JSON valide. Le test renderer-path à ajouter doit démontrer que le credential reste masqué et que la CVE/prose après LF ou CRLF reste byte-for-byte. Pour toute omission restante, le rendu doit aussi en donner la raison.

**Condition d’approbation :** borner le repli à une frontière de claim fiable, notamment LF/CRLF après l’extrait non fermé, et couvrir le cas via `_normalize_context` → `_render_p10_prompt`; toute autre suppression de suffixe doit rester observable avec sa raison.

## Vérifications R4 et non-régressions

- La règle de parité modulo 4 distingue les guillemets de fermeture (`1 mod 4`) des guillemets internes (`3 mod 4`) pour une couche d’échappement. Le reproducer synthétique avec guillemet interne puis `}`/`]` masque le scalaire entier dans **8/8** variantes; les antislashs finaux de 0 à 7 sont conservés exactement dans **8/8** variantes.
- Le cas de clé suivante mal formée masque `FIXTURE_AUTH` et garde `CVE-2025-31337: observed persistence`. Les valeurs non fermées suivies de `}` ou d’une virgule puis clé échappée gardent également le suffixe. Sans frontière reconnue, le repli masque jusqu’à EOF : c’est le finding ci-dessus.
- Le scanner parcourt le texte et ses index avec des curseurs monotones; aucun rescan quadratique n’a été trouvé. Mesure synthétique normalization→render : 1 600 labels / 25 613 caractères, médiane **0,003805 s**; 3 200 / 51 226, **0,007551 s** (environ ×1,98 pour ×2 en taille). La garde de 1 s est dans le test seulement (`test_invariant_proposals.py:1254-1262`); aucune limite de taille de prompt n’est ajoutée en production.
- Le diff R4 ne change ni `_SENSITIVE_NAMES`, ni `_sensitive_name`, ni `_TEXT_ASSIGNMENT`, ni les politiques G2/TLP/DNT/source/échantillon. Aucun champ `auth` générique n’est masqué indistinctement; le matcher ordinaire conserve son cas `auth=FIXTURE_PLAIN_AUTH`. Les tests gardent l’auteur innocent, les marqueurs CTI/CVE et le paramètre URL bénin `response-content-type`; le test synthétique confirme le marqueur `[REDACTED]` exact, le cas de collision avec suffixe et l’absence de décodage récursif. Le diff ajoute des assertions et remplace uniquement la construction du fixture antislash final; aucun test n’est supprimé ou affaibli.
- `P10_PROMPT_VERSION` reste `"4"` (`invariant_proposals.py:67`). La clé d’idempotence inclut la version (`:921-934`) et le caller la réutilise pour la recherche/création du tour. `NEEDS_DECISION_P10_VERSION` demeure donc une décision humaine distincte; Review & Handoff §2 précise que cette décision ne bloque pas L1. Elle n’est pas la cause du verdict REVISE.

## Commandes et résultats

Depuis `backend/`, Python 3.12, sans environnement Conda actif; `uv` utilisait `/tmp/m04-l1-uv-noproject-cache` :

Préfixe exact des commandes pytest/Ruff : `env -u CONDA_PREFIX -u CONDA_DEFAULT_ENV -u CONDA_SHLVL -u CONDA_PROMPT_MODIFIER -u _CE_CONDA -u _CE_M -u VIRTUAL_ENV PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' UV_CACHE_DIR=/tmp/m04-l1-uv-noproject-cache PATH=/home/nill/.local/bin:/usr/local/bin:/usr/bin:/bin`.

```text
uv run --no-project --python 3.12 pytest tests/test_invariant_proposals.py -q --tb=short
83 passed in 0.63s

uv run --no-project --python 3.12 pytest tests/test_invariant_proposals.py::test_render_p10_prompt_keeps_internal_escaped_quote_inside_auth_scalar tests/test_invariant_proposals.py::test_render_p10_prompt_preserves_evidence_after_malformed_adjacent_key tests/test_invariant_proposals.py::test_render_p10_prompt_bounds_unclosed_value_at_object_terminator tests/test_invariant_proposals.py::test_render_p10_prompt_bounds_unclosed_auth_before_later_evidence_quote tests/test_invariant_proposals.py::test_render_p10_prompt_preserves_delimiters_for_trailing_backslash_counts tests/test_invariant_proposals.py::test_render_p10_prompt_redacts_marker_prefix_collision_only_when_not_exact tests/test_invariant_proposals.py::test_render_p10_prompt_scans_many_unclosed_escaped_assignments_linearly tests/test_invariant_proposals.py::test_render_p10_prompt_preserves_escaped_innocent_author_and_plain_assignment tests/test_invariant_proposals.py::test_render_p10_prompt_preserves_closing_delimiter_after_trailing_backslash -q --tb=short
20 passed in 0.34s

uv run --no-project --python 3.12 ruff check src/cti_app/application/invariant_proposals.py tests/test_invariant_proposals.py
All checks passed!

uv run --no-project --python 3.12 ruff format --check src/cti_app/application/invariant_proposals.py tests/test_invariant_proposals.py
2 files already formatted

git diff --check e120a36f..b9115d0ec2bbd8d78cbd5aed8ad84144382b4a7b
exit 0, aucune sortie
```

Le groupe ciblé comprenait les tests de guillemet interne, clé adjacente mal formée, fallback non fermé, parité d’antislashs 0–7, collision du marqueur, garde de performance, auteur innocent et assignation ordinaire.

Reproducer synthétique hors dépôt (`/tmp/m04_l1_r4_adversarial.py`, normalization→render), lancé avec `env -u CONDA_PREFIX -u CONDA_DEFAULT_ENV -u CONDA_SHLVL -u CONDA_PROMPT_MODIFIER -u _CE_CONDA -u _CE_M PYTHONPATH=src:tests PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/tmp/m04-l1-uv-noproject-cache PATH=/home/nill/.local/bin:/usr/local/bin:/usr/bin:/bin uv run --no-project --python 3.12 python /tmp/m04_l1_r4_adversarial.py` : **8/8**, **8/8**, cas CVE adjacent préservé, **2/2** terminaisons structurelles préservées; collision/matcher/double échappement OK. Préservation du suffixe CVE après newline sans terminateur : **false** (le texte est supprimé jusqu’à EOF). Un premier lancement sans `PYTHONPATH` a échoué (`ModuleNotFoundError`); relancé avec le préfixe ci-dessus, il a produit les résultats et mesures indiqués.

Résumé par axe : **Standards — 1 finding MEDIUM**; **Spec — 1 finding MEDIUM** (même scénario, évalué sur chaque axe).

**Risque résiduel :** le scanner reste limité à une couche JSON échappée et aux labels sensibles préexistants. Le mode fail-closed protège contre une fuite de credential non fermé mais peut effacer du texte CTI adjacent dans le même claim; cette perte doit être bornée et signalée avant intégration locale.
