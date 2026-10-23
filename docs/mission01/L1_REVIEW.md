# M01-L1 — revue indépendante

**Base:** `058bb4af2278e3941956e523c5a1d560208de228`

**Commit revu:** `3b43c9f2fba48b98dc7c109365624eac74cf7a0e`

**Verdict:** `FAIL`

**Constats:** 0 `BLOCKER`, 1 `HIGH`, 1 `MEDIUM` (6 `LOW`; dont 1 `REJECTED`).

Revue en lecture seule du commit avec ses sources réelles. Les décisions et le
registre demandés ont été lus dans le worktree d’intégration
(`docs/mission01/LIMITS_DECISIONS.md:15,19-20,82` et
`docs/mission01/TASKS.md:44-45`; HEAD d’intégration observé
`7dc4b0ecdfaa436153e0031257f7d2f411199459`).

## Constats

### 1. [CONFIRMED] [HIGH] L’override de développement peut élargir une divulgation P10

`AUTOWORK_DEV_P10_MAX_PROMPT_BYTES` est décrit comme réservé au développement
dans `.env.example:60-61`, mais Compose l’injecte sans condition de profil ou
d’environnement (`compose.yaml:131`). Le code accepte jusqu’à 1 048 576 octets
(`backend/src/cti_app/application/invariant_proposals.py:950-970`).

P10 recalcule bien `tlp`, `do_not_submit` et `external_llm_allowed` dans
`derived_policy` (`backend/src/cti_app/domain/classification.py:39-47`), mais
choisit la route et construit la requête à partir de `external_llm_allowed`
seul (`backend/src/cti_app/application/invariant_proposals.py:246-264,742-749`).
Le chemin d’acquisition peut créer un échantillon avec
`do_not_submit=True` tout en recopiant `policy.external_llm_allowed`
(`backend/src/cti_app/application/sample_acquisition.py:572-588`). La passerelle
rejette une route externe seulement si `external_llm_allowed` est faux
(`backend/src/cti_app/application/model_gateway.py:1098-1111`). À titre de
comparaison, la garde Advisor vérifie aussi `do_not_submit` et TLP RED
(`backend/src/cti_app/application/investigation_service.py:2437-2452`).

Ainsi, lorsque l’override est augmenté et qu’un prompt dépasse 96 000 octets,
P10 peut envoyer davantage de faits à une route externe pour un état que les
autres contrôles considèrent non soumissible ou TLP RED, si son indicateur
`external_llm_allowed` reste vrai. L’omission de ces champs dans la garde P10
préexiste au commit; l’override rend cette voie plus large et Compose ne
confine pas l’augmentation au développement. C’est un écart bloquant pour
l’objectif L01 « RAISE_DEV » et la vérification de non-élargissement demandée.
**Action requise:** confiner effectivement l’override aux déploiements de
développement et appliquer à P10 la politique agrégée complète avant toute
augmentation de prompt externe.

### 2. [CONFIRMED] [MEDIUM] Une mauvaise valeur n’est détectée qu’au premier rendu P10

Sans variable, le plafond reste 96 000 octets UTF-8 (`invariant_proposals.py:950-970`);
la mesure utilise `len(prompt.encode("utf-8"))` et le refus est strictement au-delà
du plafond, sans troncature (`:1014-1021`). Une valeur invalide lève explicitement
`ProposalContractError`, mais `_p10_prompt_max_bytes()` n’est appelé qu’au rendu,
pas au démarrage (`:955-970`). Le premier usage P10 — y compris certains rejeux
compacts (`:195-200`) — peut donc échouer en production. La sélection et la
persistance du pointeur de conversation ont déjà lieu avant ce contrôle
(`:246-254`), mais aucun tour ni appel fournisseur n’est ajouté avant la fin du
rendu (`:254-264`). Le défaut absent est sûr; le retard de détection reste un
risque opérationnel de configuration.

### 3. [CONFIRMED] [LOW] Le parseur accepte des notations non canoniques

`int(configured, 10)` accepte `1_000`, `+5` et les espaces autour de `5`
(`invariant_proposals.py:959-969`). Vérification directe sur le code du commit:
ces trois valeurs deviennent respectivement `1000`, `5` et `5`. `""`, `0`,
`-1`, `1.5` et `1048577` lèvent `ProposalContractError`; `1048576` est accepté.
Le plafond numérique n’est donc pas contourné, mais l’exigence de forme stricte
et les tests correspondants ne sont pas satisfaits.

### 4. [CONFIRMED] [LOW] Les tests couvrent les frontières par défaut, pas toutes les limites d’override

Le test par défaut exerce 95 999 / 96 000 / 96 001 octets, conserve les références
de provenance, puis vérifie l’absence de nouvel appel et d’ajout de prompt au
refus (`backend/tests/test_limits_mission01.py:40-92`). Le test override accepte
100 000 et refuse 1 048 577, mais ne vérifie pas l’acceptation de 1 048 576 ni
l’absence d’ajout de prompt au refus max+1 (`:95-127`). Aucun test ne couvre la
variable vide, zéro, une valeur négative ou les formes non canoniques ci-dessus.
Le test G2 vérifie directement le résultat `UNAVAILABLE` pour zéro tour, sans
exercer l’admission complète ni compter les appels de transport (`:162-176`).
La preuve L06 de non-consommation est aussi limitée par le test à quelques
racines sélectionnées, sans `docs` (`:129-160`); la recherche complète ci-dessous
réduit ce risque pour l’état revu.

### 5. [CONFIRMED] [LOW] L’estimation mémoire est juste pour deux objets, pas pour le pic total

Le commentaire estime environ 5 MiB pour la chaîne prompt et son buffer UTF-8
(`invariant_proposals.py:946-949`). À un plafond de 1 MiB, c’est une borne
raisonnable pour ces deux objets dans le pire cas de stockage Unicode CPython
sur quatre octets par caractère, plus le buffer encodé. Ce n’est pas une borne
du pic total du rendu: `facts`, les lignes intermédiaires et le snapshot restent
également vivants. Le snapshot canonique a séparément une limite de 200 000
caractères (`:69,2265-2282`). Le commentaire le précise en partie, mais ne mesure
pas ces intermédiaires.

### 6. [CONFIRMED] [LOW] Retrait L06 sans consommateur actif trouvé

Au commit cible, la recherche exacte des deux anciens noms dans tout l’arbre
suivi ne trouve que leurs littéraux dans le test de régression
(`test_limits_mission01.py:132-133`). Aucun consommateur backend/frontend/script
ni profil Compose ne subsiste; l’injection Compose et les entrées `.env.example`
ont été retirées par le patch. Le playbook actif confirme le retrait et le
maintien de la garde G2 (`docs/operations/operator-playbook-medium.md:107`).
Dans les documents Mission 01 de l’intégration, les occurrences restantes
décrivent l’état initial S0 ou la décision de retrait (`BASELINE.md:1-3,61`,
`LIMITS_DECISIONS.md:20,82`); elles ne définissent pas une limite runtime.
Je n’ai trouvé aucun document actif présentant « advisor max 100 » comme une
limite appliquée. La mention `env=100` de `LIMITS_DECISIONS.md:19` est explicitement
rapportée comme non consommée par le code. Aucun profil ou document actif ne
semble cassé par le retrait.

### 7. [REJECTED] [LOW] Le plafond par défaut ou les protections d’output ne sont pas supprimés

Le défaut est toujours 96 000 octets; un prompt trop grand est refusé plutôt
que tronqué (`invariant_proposals.py:950-970,1014-1021`). Une valeur
`external_llm_allowed=False` ne peut pas forcer une route externe, et la garde
de passerelle reste active (`invariant_proposals.py:246-264`,
`model_gateway.py:1098-1111`). Le changement ne modifie pas le parsing aval ni le
passage des propositions par P09 (`invariant_proposals.py:265-280`). Ce constat
n’efface pas l’écart de politique P10 décrit au constat 1.

### 8. [PLAUSIBLE] [LOW] Simplicité et style

Le helper de validation est petit et justifié; aucune abstraction ou machinerie
inutile n’a été ajoutée. L’accès direct par `os.getenv` contourne le modèle
central `Settings` (`invariant_proposals.py:955-970`, `backend/src/cti_app/config.py:109-111`),
ce qui est une légère divergence de style, sans impact fonctionnel démontré.
Le dépôt utilise aussi des lectures directes de l’environnement dans
`backend/src/cti_app/application/typst_paths.py:15-20`.

## Exécution et limites

Le worktree de revue est resté au SHA de base. J’ai extrait le commit cible
dans un répertoire temporaire sous `/tmp` et utilisé le symlink `.venv` validé;
aucun symlink `.autowork-runs` n’a été nécessaire. Aucun test ne contacte de
fournisseur, PostgreSQL, Docker ou le réseau; les chemins testés emploient les
fakes en mémoire de `backend/tests/test_invariant_proposals.py:205-284,372-394`.

- `PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/test_limits_mission01.py -q --tb=short` — **4 passed** en 0,88 s.
- `.venv/bin/ruff check --no-cache src/cti_app/application/invariant_proposals.py tests/test_limits_mission01.py` — **All checks passed**.
- Sonde directe de `_p10_prompt_max_bytes()` — valeurs et erreurs décrites au constat 3.
- Mypy non exécuté.

**UNKNOWN:** valeur d’environnement réellement déployée, profils runtime actifs et
route provider effective; aucun répertoire runtime/secret n’a été inspecté.
**Coût:** UNKNOWN.
