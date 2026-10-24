# Q1 — Corpus offline d’évaluation du slop advisor

## But et périmètre

Ce lot fixe un corpus versionné et un oracle pytest déterministe pour mesurer
des défauts structurels dans des réponses synthétiques. Le corpus contient 16
cas, soit un exemple négatif et un contrôle positif pour chacune des huit
classes. Les identifiants, sources et SHA-256 sont fictifs. Aucun contenu CTI,
échantillon réel, octet de fichier, secret ou URL n’est inclus.

Le test appelle directement les parseurs et le préflight locaux. Il ne crée pas
de transport, ne soumet pas de prompt, ne contacte aucun modèle et n’utilise ni
réseau ni base de données. Pour Bridge, il appelle
`parse_agent_turn_decision()` puis
`validate_decision_hypothesis_references()`. Pour l’advisor, il appelle
`parse_typed_advisor_response()`, `build_advisor_dossier()` et
`evaluate_advisor_evidence_preflight()` sur les mêmes chaînes fixes du corpus.

## Fichiers et schéma

- `backend/tests/fixtures/advisor_eval/anti_slop.v1.jsonl` contient un objet
  JSON par ligne.
- `backend/tests/fixtures/advisor_eval/manifest.v1.json` fixe la version, le
  nombre de cas et le SHA-256 des octets exacts du JSONL.
- `backend/tests/test_advisor_anti_slop_eval.py` valide le manifeste, le
  contenu synthétique, les alias, les résultats par cas et les seuils; il
  affiche aussi un rapport JSON compact avec la variante, les résultats
  attendus et observés par cas, et les agrégats. La variable
  `ANTI_SLOP_EVAL_VARIANT` choisit `baseline` (défaut) ou `patch`.

Chaque cas porte `schema`, `case_id`, `consultation_type` (`bridge` ou
`advisor`), `sources`, `context`, `allowed_aliases`, `response_text`, `labels`
et `expected`. `expected` conserve le statut du parseur, le statut du
préflight, l’utilisabilité et les métriques du cas. Les alias advisor A1/A2 et
les références d’hypothèse Bridge sont comparés aux alias autorisés dérivés du
contexte synthétique.

Les phrases factuelles sont annotées dans `labels.fact_sentences`. L’oracle
compare ces annotations au texte réellement exposé par le parseur; il ne juge
pas si une phrase est vraie. Une phrase est structurellement ancrée si son
assertion résolue référence une observation présente avec le même SHA-256
canonique de forme valide.

## Métriques et seuils

Aucun score global n’est calculé. Chaque taux affiche son numérateur, son
dénominateur et son seuil.

| Dimension | Formule opérationnelle | Seuil |
|---|---|---:|
| Références invalides | réponses utilisables malgré un alias advisor ou une référence Bridge inconnue / cas annotés `invalid_reference` | 0 |
| Ancrage structurel | phrases factuelles exposées et liées à une assertion avec observation/SHA correspondants / phrases factuelles exposées par les parseurs | 1,0 |
| Absence de preuve | cas sans preuve où le parseur donne une réponse utilisable ou où le préflight permettrait la soumission / cas annotés `no_proof` | 0 |
| Contradiction et UNKNOWN | réponses qui citent deux observations du même sujet et de la même propriété, dont les valeurs diffèrent, et indiquent `UNKNOWN` ainsi qu’une limite / cas annotés `contradiction_targeted` | 1,0 |
| Duplication exacte | répétitions factuelles après NFKC, casefold, réduction des espaces et retrait de la ponctuation finale / phrases factuelles des contrôles positifs | 0 |
| Remplissage générique | clauses correspondant à la liste versionnée / clauses des contrôles positifs | 0 |
| Overclaim synthétique | négatifs annotés overclaim qui restent utilisables / négatifs annotés `synthetic_overclaim` | 0 |
| Contrôle positif sain | contrôles sains parseables, ancrés, sans remplissage ni répétition / contrôles annotés `healthy_positive` | 1,0 |

Le dénominateur de contradiction ne comprend que le cas qui associe deux
observations au même sujet et à la même propriété avec des valeurs distinctes,
puis est explicitement annoté `contradiction_targeted`. Le cas cible utilise le
même fichier synthétique `sample-synth.bin` et la propriété `file_size_bytes`,
avec des relevés de 128 et 256 octets. Le test vérifie ces métadonnées, leur
correspondance avec les assertions citées, les deux références et la reprise
des deux valeurs (128 et 256) dans la réponse; il vérifie aussi `UNKNOWN` et une
limite explicite. Le contrôle à source unique
vérifie qu’une contradiction n’est pas inventée, mais n’entre pas dans ce taux.
Pour les répétitions et le remplissage, le dénominateur se limite aux contrôles
positifs, comme demandé.

Le contrôle de fuite parcourt les chaînes après décodage JSON et rejette aussi
les chaînes Base64-like isolées d’au moins 200 caractères. Son contrôle des
types `bytes`/`bytearray`/`memoryview` est lui aussi post-décodage JSON : il ne
détecte pas des octets bruts dans le fichier source. Le corpus est du JSON UTF-8
synthétique et ne contient aucun contenu binaire.

La liste générique est `generic-phrases.v1` :

- `Il est recommandé d'approfondir l'analyse.`
- `Il faut rester vigilant.`
- `Further research is needed.`

Ces correspondances sont exactes après normalisation; la liste n’est pas un
classifieur général de conseils vagues.

## Résultats baseline

Mesure des 16 réponses fixes au code source de base
`b2a5c57a04f1121ee784a75ffbea2f96177980f3` :

| Dimension | Mesure | Seuil |
|---|---:|---:|
| Références invalides | 0/2 = 0,0 | 0 |
| Ancrage structurel | 10/10 = 1,0 | 1,0 |
| Absence de preuve | 0/2 = 0,0 | 0 |
| Contradiction et UNKNOWN | 1/1 = 1,0, même fichier/propriété avec tailles 128/256 | 1,0 |
| Duplication, contrôles positifs | 0/2 = 0,0 | 0 |
| Remplissage, contrôles positifs | 0/1 = 0,0 | 0 |
| Overclaim synthétique | 0/1 = 0,0 | 0 |
| Contrôle positif sain | 1/1 = 1,0 | 1,0 |

Le préflight retourne `INSUFFICIENT_EVIDENCE` pour les deux cas sans preuve.
Le parseur rejette le finding Bridge `supported` sans observation. Le contrôle
positif advisor sain est utilisable; le contrôle négatif sain ne l’est pas.
Les résultats de chaque cas et de chaque classe sont réémis par le test.

Empreintes calculées sur des prompts rendus depuis des contextes synthétiques
stables :

- Investigation, `INVESTIGATION_PROMPT_VERSION`
  `investigation-agent-turn.v1` — SHA-256
  `4b775cab6d5d03fd30eee6c18981efc7ad20ffffd732b248e066e4649fbb2f71`.
- Advisor, constantes `ADVISOR_SCHEMA_VERSION=advisor-consult.v1`,
  `ADVISOR_WIRE_VERSION=advisor-wire.v1`,
  `ADVISOR_RESPONSE_SCHEMA_V3=advisor-response.v3` et
  `ADVISOR_SOURCE_PARSER_VERSION=advisor-source-parser.v3` — SHA-256 du prompt
  rendu `54c9f454f0e80c95e518279c6a3205a776714db84964039a1fdaa0c7ebecafab`.
- Corpus `anti_slop.v1`, 16 cas — SHA-256 exact
  `a73fdb97e4ec9503242af8c233af40afca9804383bf5697695ea9a90956d684e`.

## Comparaison Q2 et limites

Pour Q2, conserver ces cas, les chaînes `response_text` et le SHA du corpus,
puis exécuter le même test avant et après le changement de prompt. Émettre la
variante `baseline` pour Q1 et `patch` pour Q2. Comparer les empreintes rendues,
les versions advisor et les résultats par cas et par classe. Les réponses de ce
corpus restent fixes : les métriques identiques ne prouvent pas
que le prompt patched réduit le slop produit par un modèle. Elles vérifient le
comportement structurel des parseurs et des contrôles sur un jeu reproductible.

Il n’y a pas de juge sémantique. L’overclaim est un négatif synthétique dont la
forme échoue au contrat existant; ce résultat ne généralise pas aux affirmations
plausibles mais fausses. La paire de contradiction vérifie une incohérence de
métadonnées synthétiques et les références citées; les marqueurs `UNKNOWN` et de
limite restent des proxys lexicaux. Les formulations génériques sont aussi des
proxys lexicaux. Un
`ScriptedTurnTransport`, même déterministe, ne démontrerait pas une baisse du
slop réel d’un fournisseur. Ce livrable n’ajoute aucune dépendance et ne modifie
aucun code de production : quatre fichiers, zéro dépendance.
