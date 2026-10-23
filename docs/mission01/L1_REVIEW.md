# M01-L1 — revue indépendante

**Base:** `058bb4af2278e3941956e523c5a1d560208de228`

**Commit initial examiné:** `3b43c9f2fba48b98dc7c109365624eac74cf7a0e`

**Verdict initial (R1):** `FAIL` — remplacé par la re-revue R2 ci-dessous.

**Verdict R2:** `FAIL` — remplacé par les re-revues R3 et R4 ci-dessous.

**Verdict R3:** `FAIL` — historique; voir les re-revues R4 et R5 ci-dessous.

**Verdict R4:** `FAIL` — 2 `HIGH`, 0 `MEDIUM` sur le relèvement P10 abandonné.

**Verdict final R5 (périmètre L06):** `PASS_WITH_CHANGES` — le repli L06
est conforme; la commande ciblée exécute 2 tests, pas les 43 annoncés.

**Constats R1:** 0 `BLOCKER`, 1 `HIGH`, 1 `MEDIUM` (6 `LOW`; dont 1 `REJECTED`).

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

## Re-revue après 0194648

**Delta examiné:** `3b43c9f2fba48b98dc7c109365624eac74cf7a0e..01946483dba171a3c468c1b14c8ea8ae0b8931d1`.
Le correctif ferme plusieurs constats R1, mais un chemin de divulgation reste
hors de la nouvelle agrégation. Verdict final R2 : **`FAIL` — 1 HIGH, 0 MEDIUM, 6 LOW**.

### Constats reclassés

1. **[CONFIRMED] [HIGH] La garde agrège les Samples, pas la politique des Sources des claims envoyés à P10.**

   La nouvelle garde refuse correctement la politique calculée avant rejeu,
   sélection de route et ajout de tour (`backend/src/cti_app/application/invariant_proposals.py:171-176,184-188,256-276`).
   Cette politique est toutefois calculée uniquement à partir des Samples
   d’origine retenus (`:474-487,520-531`; agrégation à
   `backend/src/cti_app/domain/classification.py:39-47`). En parallèle, P10
   charge tous les claims du sujet sans joindre leur SourceDocument
   (`invariant_proposals.py:399-403`; dépôt
   `backend/src/cti_app/infrastructure/database/repositories/collection.py:185-191`),
   les insère dans le snapshot (`invariant_proposals.py:579-582`) et les rend
   dans le prompt (`:1079-1093`). Un Claim porte un `source_document_id`
   (`backend/src/cti_app/domain/collection.py:559-573`) et chaque SourceDocument
   a ses propres `tlp`, `do_not_submit` et `external_llm_allowed`
   (`backend/src/cti_app/domain/entities.py:67-76`). Aucun contrôle P10 ne
   récupère ni n’agrège cette politique de source.

   Donc un Sample vert autorisé peut faire passer la garde tandis qu’un claim
   issu d’une source RED, `do_not_submit` ou interdite au modèle est tout de
   même inclus dans la requête externe. Avec le plafond de développement élevé,
   davantage de ces faits peuvent tenir dans le prompt. Le test d’agrégation
   élève le plafond et couvre les seuls champs du Sample (`backend/tests/test_limits_mission01.py:173-203`);
   le faux dépôt de claims ne fournit aucun état de politique de source
   (`backend/tests/test_invariant_proposals.py:123-126`). Il manque un refus
   avant transport pour un claim dont la SourceDocument est restrictive.

   **Action requise:** inclure la politique de chaque source de contenu rendu
   dans l’admission P10, ou exclure/refuser les claims dont la politique ne
   permet pas cette soumission, puis tester zéro routage/transport. Ce défaut
   laisse un chemin de divulgation provider non couvert et empêche de déclarer
   le correctif sûr.

2. **[CONFIRMED] [LOW] L’override est confiné, avec un cas de configuration fail-closed distinct du défaut.**

   La construction du service capture la limite (`invariant_proposals.py:145`).
   Sans `AUTOWORK_DEV_LIMITS_ENABLED=1`, toute valeur de plafond est ignorée et
   96 000 s’applique (`:970-972`; test `backend/tests/test_limits_mission01.py:150-160`).
   L’override nécessite aussi `APP_ENV=development`; le plafond est alors
   limité à 1 048 576 (`:973-988`). Compose et `.env.example` n’injectent plus
   la variable (`compose.yaml:126-133`, `.env.example:54-67`), et la doc active
   décrit les deux conditions (`docs/model_conversations.md:74-81`).

   Nuance vérifiée : avec le drapeau exactement égal à `1` mais un autre
   `APP_ENV`, le constructeur lève une `ProposalContractError` (`:973-974`,
   test `test_limits_mission01.py:163-170`) au lieu de retomber sur 96 000.
   Cela ne permet pas d’élargir le prompt, mais une configuration résiduelle
   du drapeau peut rendre P10 indisponible à sa construction.

3. **[CONFIRMED] [LOW] La validation stricte et les frontières demandées sont réparées.**

   La regex n’accepte que les décimaux canoniques positifs ASCII et la plage
   est contrôlée à la construction (`invariant_proposals.py:975-988,145`).
   Les tests refusent `""`, `0`, `-1`, `1_000`, `+5`, espaces, `1.5` et
   `1048577` (`test_limits_mission01.py:134-147`). La valeur max exacte
   `1048576` est acceptée; max+1 est refusé explicitement avant ajout de tour
   ou appel fournisseur (`:97-131`). Le défaut garde N−1/N/N+1 en octets UTF-8
   et refuse sans troncature (`:41-95`; code `invariant_proposals.py:1036-1045`).

4. **[PLAUSIBLE] [LOW] L’estimation d’environ 5 MiB ne borne pas le pic de rendu complet.**

   À 1 MiB UTF-8 maximum, l’ordre de grandeur est raisonnable pour la seule
   chaîne prompt (jusqu’à environ 4 MiB si les caractères sont stockés sur
   quatre octets dans CPython) et son buffer d’encodage de 1 MiB
   (`invariant_proposals.py:959-962,1012-1036`). `facts` et les chaînes
   intermédiaires peuvent aussi être vivants pendant la concaténation; le
   commentaire exclut le snapshot mais ne quantifie pas ces intermédiaires.
   C’est une estimation limitée, pas une borne mémoire du rendu total.

5. **[REJECTED] [LOW] Le delta n’affaiblit pas les protections existantes de G2, TLP ou du routage.**

   La garde de Sample est ajoutée avant rejeu/routage/transport; les contrôles
   de sortie et de passage par P09 ne sont pas changés dans le delta. Le test
   G2 existant reste présent (`test_limits_mission01.py:239-253`). Le test
   élevé couvre la combinaison Sample `external_llm_allowed=True` et
   `do_not_submit=True`, puis TLP RED, sans transport/prompt/conversation/pivot
   (`test_limits_mission01.py:173-203`); le chemin Sample correspond à celui
   produit par l’acquisition (`backend/src/cti_app/application/sample_acquisition.py:572-588`).
   Les attentes de routage local mises à jour renforcent le refus lorsque
   `external_llm_allowed` est faux (`test_invariant_proposals.py:448-459,779-786`).
   Une révocation bloque aussi le rejeu avant transport (`:684-698`).
   Cette conclusion ne neutralise pas le chemin de divulgation par claims au
   constat 1.

6. **[CONFIRMED] [LOW] L06 et les mentions Advisor restent propres.**

   Le delta retire encore l’injection P10 de Compose et `.env.example`;
   aucune occurrence active des deux variables Advisor retirées ni de
   « advisor max 100 » n’a été trouvée dans le code, les scripts, Compose ou
   les docs actives. Les références restantes aux anciens réglages sont des
   tests/explications de retrait; le changement documentaire P10 se limite
   à `docs/model_conversations.md:74-81`.

7. **[PLAUSIBLE] [LOW] Le changement reste simple, avec une petite divergence de style.**

   La validation est un helper borné et lisible; `os.getenv` direct reste
   cohérent avec des usages directs existants mais diverge du `Settings`
   central. Pas de machinerie inutile ni d’élargissement de périmètre détecté
   dans le delta de six fichiers.

### Exécution R2

Les commandes ont été exécutées sur une archive du commit cible
`01946483dba171a3c468c1b14c8ea8ae0b8931d1` sous `/tmp`, avec le `.venv`
validé du worktree de l’implémenteur. Aucun symlink `.autowork-runs` n’était
nécessaire. Les tests utilisent des fakes en mémoire; aucun fournisseur,
PostgreSQL, Docker ou réseau n’a été contacté.

- `PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/test_limits_mission01.py tests/test_invariant_proposals.py -q --tb=short` — **58 passed** en 1,13 s.
- `.venv/bin/ruff check --no-cache src/cti_app/application/invariant_proposals.py tests/test_limits_mission01.py tests/test_invariant_proposals.py` — **All checks passed**.
- Aucun test n’exerce un Claim relié à une SourceDocument restrictive; ce cas est le bloqueur non couvert décrit au constat 1.

**UNKNOWN:** politiques des Sources et variables d’environnement effectivement
présentes dans les déploiements; l’utilisation runtime réelle de P10 et le
provider configuré. Aucun provider ou profil runtime n’a été inspecté.
**Coût R2:** UNKNOWN.

## Re-revue après a3d17f8

**Delta examiné:** `01946483dba171a3c468c1b14c8ea8ae0b8931d1..a3d17f81bdd26bbc8f50e16668762cc47c1ec1c4`.
Le contrôle direct des claims bornés ferme le HIGH R2 pour ces claims, mais
deux chemins de contenu dérivé/historique échappent encore à cette résolution.
Verdict final R3 : **`FAIL` — 2 HIGH, 0 MEDIUM, 1 LOW**.

### Constats reclassés

1. **[CONFIRMED] [HIGH] Les invariants déjà persistés peuvent divulguer des données de claims dont la source n’est pas agrégée.**

   Le nouveau chemin résout les SourceDocuments des seuls claims présents dans
   `context.report_claims` après bornage (`backend/src/cti_app/application/invariant_proposals.py:575-591`).
   Or P10 charge séparément les invariants existants, leurs provenances et
   leurs patterns (`:389-409,527-535,573-580`). P09 accepte une
   `ReportClaimProvenance` sur un candidat et la transmet au registre
   (`backend/src/cti_app/application/invariants.py:425-438`;
   `backend/src/cti_app/application/invariant_proposals.py:929-937`;
   type canonique à `backend/src/cti_app/domain/invariants.py:206-223`). Le
   contexte des invariants inclut `pattern` et provenance (`invariant_proposals.py:2248-2261`)
   et le rendu P10 inclut les invariants et le manifeste des claims
   (`:1049-1059,1080-1094,1158-1171`).

   Les claims sont d’abord plafonnées séparément des invariants à 64
   (`:407-415`), puis le bornage peut couper `report_claims` à 32 tout en
   conservant `existing_invariants` (`:2346-2363`). Un invariant provenant
   d’une claim RED, `do_not_submit` ou externe-interdite peut donc laisser son
   pattern et son identifiant de claim dans le prompt sans que la source soit
   résolue par `_claim_source_documents`.

   **Action requise:** inclure les `source_document` de toutes les
   `ReportClaimProvenance` présentes dans le contexte final — notamment les
   invariants existants — dans l’agrégation, ou retirer/refuser ces éléments
   quand la politique source ne peut être établie. Ajouter un test avec un
   invariant existant référant une claim restrictive absente de la liste
   `report_claims` finale et prouver zéro conversation/transport.

2. **[PLAUSIBLE] [HIGH] Un rejeu historique peut réutiliser une snapshot de claims non vérifiée par la politique courante.**

   À chaque appel, la politique courante est recalculée avant la recherche de
   rejeu (`invariant_proposals.py:162-188`). Toutefois le rejeu d’un ancien
   prompt contenant `BEGIN_P10_SNAPSHOT_DATA` reparcourt sa snapshot persistée
   (`:199-218`) et en passe les candidats à P09 (`:223-243`). La clé
   d’idempotence n’inclut pas le `context` (`backend/src/cti_app/domain/invariant_proposals.py:287-289`;
   construction à `invariant_proposals.py:942-955`). Si un claim présent dans
   l’ancien prompt a depuis disparu ou a été écarté du contexte courant borné,
   sa source n’est pas dans l’agrégation courante, alors que l’ancien résultat
   peut encore être rejoué et traité. Ce chemin ne fait pas un nouvel appel
   fournisseur, mais permet de réutiliser une sortie produite à partir d’un
   contexte dont la politique n’a pas été revérifiée.

   **Action requise:** avant de réutiliser un tour historique, résoudre et
   agréger aussi les sources de claims de la snapshot persistée; tester un
   tour historique dont le claim restreint n’apparaît plus dans le contexte
   courant.

3. **[CONFIRMED] [LOW] Le chemin direct des claims est fail-closed, mais quelques cas exigés ne sont pas testés explicitement.**

   Le helper refuse liste/repository/ID manquants, exceptions ou source
   absente, sujet différent et champs de politique absents ou invalides
   (`invariant_proposals.py:2290-2343`). Les tests couvrent les trois états
   restrictifs avec plafond maximal, source absente, `tlp=None`, puis un
   rejeu où la source devient RED (`backend/tests/test_limits_mission01.py:222-359`).
   Je n’ai trouvé aucun test qui donne une claim sans `source_document_id`,
   une source d’un autre sujet ou une erreur du dépôt; l’implémentation
   correspondante lève bien avant sélection de route. C’est une lacune de
   preuve, distincte des deux chemins HIGH ci-dessus.

4. **[REJECTED] [LOW] Pas de régression du plafond ou du confinement de développement.**

   Le défaut reste 96 000 octets; seules les deux conditions exactes
   `AUTOWORK_DEV_LIMITS_ENABLED=1` et `APP_ENV=development` autorisent le
   plafond de développement, borné à 1 048 576 (`invariant_proposals.py:963-988`).
   Hors de ce cas l’override est ignoré et le défaut s’applique
   (`test_limits_mission01.py:150-186`). Le parse décimal strict, les cas
   invalides et les frontières testées en R2 restent inchangés
   (`test_limits_mission01.py:41-160`). Le commentaire mémoire précise
   désormais que `facts` et les intermédiaires s’ajoutent (`invariant_proposals.py:959-962`).

5. **[REJECTED] [LOW] Aucun élargissement G2/L06 ou du périmètre dans le delta.**

   Le delta touche uniquement `invariant_proposals.py` et ses deux tests; il
   ne modifie pas Compose, `.env.example`, ni les consommateurs Advisor retirés.
   Le test G2 reste présent (`test_limits_mission01.py:395-409`), et les tests
   ciblés couvrent les refus TLP, `do_not_submit`, routage local et rejeu.
   La garde de source ne modifie pas les contrôles de sortie ni le passage
   P09. Le constat HIGH reste toutefois bloquant pour les sources de claims
   dérivées décrites aux constats 1 et 2.

### Exécution R3

Tests exécutés dans le worktree de l’implémenteur au SHA
`a3d17f81bdd26bbc8f50e16668762cc47c1ec1c4`, avec le `.venv` du worktree.
Les fakes en mémoire sont utilisés; aucun fournisseur, PostgreSQL, Docker ou
réseau n’a été contacté.

- `PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/test_limits_mission01.py tests/test_invariant_proposals.py -q --tb=short` — **69 passed** en 0,77 s. Pytest a émis un avertissement de cache en lecture seule (`.pytest_cache`); le code de retour est 0.
- `.venv/bin/ruff check --no-cache src/cti_app/application/invariant_proposals.py tests/test_limits_mission01.py tests/test_invariant_proposals.py` — **All checks passed**.
- Aucun test ne couvre un invariant persistant avec `ReportClaimProvenance` hors de `report_claims` bornés, ni le rejeu d’une ancienne snapshot contenant un claim devenu absent ou tronqué.

**UNKNOWN:** volume et politique réels des claims/invariants dans les
déploiements, présence de tours historiques au format snapshot complet, et
provider runtime. Aucun état runtime ou secret n’a été inspecté.
**Coût R3:** UNKNOWN.

## Re-revue finale après 8d7c969

**Delta examiné:** `a3d17f81bdd26bbc8f50e16668762cc47c1ec1c4..8d7c96994a0e634f01c4dbff8e14968b757bd85f`.
La réparation ferme le cas direct d’un invariant hors de `report_claims` et
revalide les sources des snapshots historiques marquées. Deux chemins restent
sans barrière démontrée; verdict final R4 : **`FAIL` — 2 HIGH, 0 MEDIUM**.

### Constats reclassés

1. **[CONFIRMED] [HIGH] Le rejeu des tours markerless ne réévalue pas les sources du contexte historique.**

   Les prompts courants n’embarquent pas de snapshot canonique; le test confirme
   l’absence du marqueur `BEGIN_P10_SNAPSHOT_DATA`
   (`backend/tests/test_invariant_proposals.py:552-571`), et le rendu n’écrit
   que les faits (`backend/src/cti_app/application/invariant_proposals.py:1045-1066`).
   Au rejeu, le code traite le snapshot courant comme s’il était celui du tour
   persisté et régénère ses alias depuis le contexte courant
   (`invariant_proposals.py:202-210`). La clé d’idempotence exclut explicitement
   `context` (`backend/src/cti_app/domain/invariant_proposals.py:287-289`), donc
   une claim ou une provenance source peut disparaître du contexte sans changer
   la clé qui retrouve le tour précédent. Dans ce cas, `_historical_claim_sources`
   n’est appelé que pour les entrées portant l’ancien marqueur
   (`invariant_proposals.py:223-228`); le chemin markerless passe au parsing,
   à l’archivage et à P09 avec le snapshot courant (`:238-258`). Le contrôle
   courant protège les références qui subsistent, mais ne peut prouver la
   politique des sources présentes seulement dans le prompt historique.

   Le test de rejeu courant rend une source RED tout en la laissant présente
   dans le contexte actuel (`backend/tests/test_limits_mission01.py:563-583`);
   il ne teste pas une source retirée/absente du contexte avant le rejeu. Le
   test de snapshot historique absente force lui-même le format ancien marqué
   (`:388-425`). Je n’affirme pas qu’un transport externe est réémis au rejeu;
   le défaut est que le contrôle de politique historique demandé n’est pas
   appliqué avant la réutilisation et le passage possible à P09. La sortie
   persistée est parsée avec les alias recalculés du contexte courant
   (`invariant_proposals.py:241-243`), pas avec une table d’alias conservée
   avec le tour.

   **Action requise:** lier le tour idempotent à un instantané conservé des
   références de contexte, puis recharger leur politique courante avant tout
   parsing/réutilisation/P09; ajouter un test markerless où une source de
   l’ancien tour devient absente ou non résoluble et vérifier zéro appel P09.

2. **[CONFIRMED] [HIGH] La borne de 256 provenances peut laisser passer un pattern dont une source restrictive a été tronquée.**

   `_invariant_context` conserve les provenances canoniques de chaque invariant
   et échoue si l’une est invalide (`invariant_proposals.py:2279-2294`). Mais
   `_bound_context` passe récursivement les listes à `_bounded_json`, qui ne
   garde que les 256 premiers éléments (`:2454-2471,2511-2518`). L’extraction de
   politique parcourt ensuite seulement ce contexte borné
   (`:2396-2440`), tandis que le rendu conserve le `pattern` de l’invariant et
   affiche les invariants existants (`:1111-1125`; le format de ligne omet
   seulement le champ `provenances`, `:1205-1230`). Le catalogue de provenance
   est lui aussi une liste bornée à 256 après création (`:591-597`).

   Le modèle `CandidateInvariant` n’impose pas de cardinalité maximale aux
   provenances (`backend/src/cti_app/domain/invariants.py:395-467`), le service
   P09 accepte une séquence sans limite correspondante
   (`backend/src/cti_app/application/invariants.py:92-107`), et le dépôt stocke
   une ligne pour chaque provenance (`backend/src/cti_app/infrastructure/database/repositories/invariants.py:197-220`).
   Un invariant à plus de 256 références valides peut donc avoir une
   `ReportClaimProvenance` restrictive après la borne: sa source ne participe
   pas à la garde alors que son pattern reste dans le texte P10. Aucun test R4
   ne couvre cette cardinalité.

   **Action requise:** ne jamais tronquer une liste de provenances de contenu
   sans refuser le contexte ou résoudre toutes ses références avant bornage;
   tester une provenance restrictive après le 256e élément et vérifier refus
   avant conversation/routage/transport.

3. **[CONFIRMED] [LOW] Les cas R3 visibles dans le contexte borné sont maintenant fail-closed et testés.**

   La résolution descend les `report_claims` et tous les mappings/listes du
   contexte final; elle vérifie claim, sujet, lien vers la source, identité,
   sujet de la SourceDocument et types/politique TLP
   (`invariant_proposals.py:2323-2440`). `_invariant_context` refuse les
   provenances non canoniques (`:2279-2287`). Le test avec une provenance
   d’invariant hors `report_claims`, au plafond de développement maximal et
   source RED, affirme zéro conversation, prompt, transport et pointeur de
   conversation (`backend/tests/test_limits_mission01.py:333-384`). Les tests
   couvrent aussi claim absente, `source_document_id` absent, sujet de source
   différent et erreur de dépôt avec zéro transport (`:487-560`). Le rejeu
   portant explicitement un snapshot marqué recharge la politique et refuse
   une source devenue RED avant un second P09/transport (`:429-483`). Ces
   résultats ferment les scénarios représentés, pas les deux contournements
   décrits ci-dessus. Le rejeu historique testé couvre la transition vers RED;
   DnS, externe interdit et erreurs de résolution ne sont pas chacun exercés
   sur ce chemin historique.

4. **[REJECTED] [LOW] Aucun retour du défaut P10, de l’override hors développement, de L06 ou d’un changement B0A dans le delta.**

   Le delta ne touche que `backend/src/cti_app/application/invariant_proposals.py`
   et les deux fichiers de tests (`git diff --name-only
   a3d17f8..8d7c969`). Le défaut 96 000 octets, le parseur décimal strict et
   les deux conditions de confinement du plafond de développement ne sont pas
   modifiés; les tests G2/L06 et les contrôles de politique existants restent
   dans le lot ciblé. Compose, `.env.example`, documentation active, frontend,
   scripts et fichiers B0A ne sont pas dans le delta. Aucune régression de
   routage ou élargissement additionnel n’a été trouvée.

### Exécution R4

Tests exécutés dans le worktree de l’implémenteur au SHA exact
`8d7c96994a0e634f01c4dbff8e14968b757bd85f`, avec son `.venv`. Ils utilisent
les fakes en mémoire; aucun fournisseur, PostgreSQL, Docker ou réseau n’a été
contacté.

- `PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/test_limits_mission01.py tests/test_invariant_proposals.py -q --tb=short` — **76 passed** en 0,78 s, code retour 0. Un `PytestCacheWarning` indique que le worktree cible en lecture seule ne peut écrire `.pytest_cache`.
- `.venv/bin/ruff check --no-cache src/cti_app/application/invariant_proposals.py tests/test_limits_mission01.py tests/test_invariant_proposals.py` — **All checks passed**.
- `git diff --check a3d17f8..8d7c969` — aucune erreur.

**UNKNOWN:** fréquence de rejouabilité des tours markerless, cardinalité réelle
des provenances dans les déploiements, et configuration/provider runtime. Ces
informations opérationnelles ne sont pas nécessaires pour établir les deux
chemins de code manquants; aucun runtime/secret n’a été inspecté.
**Coût R4:** UNKNOWN.

## Repli final e4772fa

**Commit examiné:** `e4772fae3f041e6e66dc087f404a8b4417854c94`, comparé à
`main@058bb4af2278e3941956e523c5a1d560208de228`.
**Verdict pour le périmètre restreint L06:** **`PASS_WITH_CHANGES`** — le
retrait des deux réglages Advisor inutilisés est cohérent et le contrôle G2
reste en place. Réserve de validation : le test ciblé exécute 2 tests, pas les
43 annoncés.

### Constats

1. **[CONFIRMED] [LOW] P10 et sa documentation sont restaurés à la base; les anciens défauts restent DEFER.**

   `backend/src/cti_app/application/invariant_proposals.py` et
   `docs/model_conversations.md` sont identiques à la base
   (comparaison ciblée de ces deux chemins avec la base, sans différence).
   Le code conserve donc le défaut P10 de 96 000 octets UTF-8 et ne contient
   aucun override ni garde partielle nouvelle (`invariant_proposals.py:945-993`).
   Aucun nom `AUTOWORK_DEV_*P10*` n’est injecté dans Compose ou `.env.example`;
   une recherche Git suivie des sources ne trouve aucun consommateur de ces
   variables. Le delta n’inclut aucun des deux fichiers P10.

   **Les défauts P10 de provenance tronquée et de rejeu markerless documentés
   en R4 (ci-dessus, constats HIGH) restent préexistants à ce patch.** Le
   correctif de relèvement P10 est abandonné et le statut est **DEFER**; cette
   restauration ne résout pas ces défauts. Ils doivent être repris dans un lot
   dédié avec un nouveau contrôle de provenance et de rejeu.

2. **[CONFIRMED] [LOW] Le delta contient uniquement les quatre changements L06 demandés.**

   `git diff 058bb4a..e4772fa --stat` donne exactement quatre chemins :
   `.env.example`, `compose.yaml`, `docs/operations/operator-playbook-medium.md`
   et `backend/tests/test_limits_mission01.py`. Compose et `.env.example`
   retirent `OPERATOR_ADVISOR_MAX_TURNS_PER_INVESTIGATION` et
   `OPERATOR_ADVISOR_MIN_CONSULTATION_INTERVAL_SECONDS`
   (`compose.yaml:125-133`, `.env.example:57-62`). Dans l’arbre suivi, les
   deux noms ne subsistent que comme littéraux des tests; aucun consommateur
   backend, frontend, script, worker ou Makefile n’est présent.

   Le playbook actif indique que ces réglages Advisor sans consommateur sont
   retirés et que le budget de tours G2 demeure la garde d’autorisation
   (`docs/operations/operator-playbook-medium.md:105-108`). La seule mention
   générique d’anciens `OPERATOR_ADVISOR_*` hors des tests est dans
   `docs/operations/correction-state-2026-10-19.md:8`, que l’index documentaire
   classe comme journal historique, non normatif (`docs/README.md:78-82`). Je
   n’ai trouvé aucun document actif qui présente « advisor max 100 » ou une
   limite locale Advisor retirée comme étant active.

3. **[REJECTED] [LOW] Aucun contrôle G2/TLP/provider n’est supprimé et aucun autre document actif ne casse.**

   Le seul changement de source Python est le nouveau test; l’implémentation
   Advisor, P10, la passerelle provider et l’agrégation TLP sont byte-à-byte
   inchangées par rapport à la base. `_check_advisor_limits` continue de
   renvoyer `UNAVAILABLE` quand le budget G2 est épuisé
   (`backend/src/cti_app/application/investigation_advisor_tools.py:1106-1143`);
   le nouveau test couvre le refus à zéro tour et vérifie qu’aucune limite
   Advisor locale n’est annoncée (`backend/tests/test_limits_mission01.py:47-61`).
   Les références documentaires actives aux modèles de conversation P10 ne
   sont pas modifiées; le playbook reste cohérent avec le retrait L06.

4. **[CONFIRMED] [LOW] La recette ciblée passe, mais son nombre réel diffère de l’annonce.**

   Le fichier ajouté ne contient que deux tests : absence de consommateurs
   (lignes 15-45) et garde G2 (lignes 47-61). La commande demandée a rendu
   **2 passed**; elle n’a pas rendu les **43 passed annoncés**. Ces deux tests
   sont pertinents pour L06, et la recherche Git indépendante couvre aussi
   les documents suivis, mais la différence de compte doit rester explicite.

### Exécution R5

Exécution dans le worktree de l’implémenteur au SHA
`e4772fae3f041e6e66dc087f404a8b4417854c94`, avec `.venv`; aucun fournisseur,
PostgreSQL, Docker ou réseau n’a été contacté.

- `PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/test_limits_mission01.py -q --tb=short` — **2 passed** en 0,52 s, code retour 0. Pytest signale seulement que le worktree cible en lecture seule ne peut écrire `.pytest_cache`.
- `.venv/bin/ruff check --no-cache src/cti_app/application/investigation_advisor_tools.py tests/test_limits_mission01.py` — **All checks passed**.
- `git diff --check 058bb4a..e4772fa` — aucune erreur; `git diff --stat` — exactement les quatre fichiers L06 ci-dessus.
- Comparaison ciblée avec la base — P10 (`invariant_proposals.py`, `docs/model_conversations.md`), Advisor, passerelle provider et agrégation de classification inchangés.

**UNKNOWN:** l’état réel de `.env` et du déploiement n’a pas été inspecté; les
références externes ou privées aux anciennes variables ne sont pas vérifiées.
Le compte « 43 passed » ne correspond pas à la commande et au fichier du
commit; aucune source alternative de ce compte n’a été fournie.
**Coût R5:** UNKNOWN.
