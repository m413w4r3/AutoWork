# Capacités d'analyse — état réel au 2026-10-17

Établi par l'orchestrateur sur la stack déployée (`make up`, sans reset), à partir de l'inventaire runtime de l'API
(`app.state.investigation_tool_inventory`) et de preuves d'exécution. Détail des lots : `nimbalyst-local/orchestration-2026-10-17.md`.

## Frontière d'isolation
`bwrap` est installé dans les images backend/worker mais **inopérant en conteneur** (user namespaces refusés : `unshare_probe_failed`).
Les conteneurs backend/worker (root) n'exécutent donc **aucun** octet non fiable. Toute analyse d'échantillon/capture est déléguée au
conteneur `analysis-worker` (UID 10001, rootfs en lecture seule, `cap_drop: ALL`, `no-new-privileges`, réseau `analysis-internal` sans
egress) via `ConfinedAnalysisExecutionPort` (jobs Dramatiq `analysis`, probe `analysis-probe`). Le port n'est « disponible » qu'après
attestation (uid, capacités effectives nulles, rootfs RO, profil `bookworm-internal-v1`, accès PostgreSQL/MinIO, versions épinglées).

## Tableau des capacités
| Capacité | Statut | Preuve |
|---|---|---|
| Bridge / `InvestigationService` | OPÉRATIONNEL ET TESTÉ | runs réels 3/4 (sessions précédentes) ; non rejoué dans ce chantier |
| VirusTotal : report / relations / behaviours / search | OPÉRATIONNEL ET TESTÉ (réserve) | actif ; file report via repli direct v2 (le proxy 404 sur `/files/{hash}`) |
| VirusTotal : `download_sample` | BLOQUÉ PAR PRÉREQUIS EXTERNE | le proxy renvoie 404 sur `/files/{hash}`, `/download`, `/download_url` (non rejoué ici) |
| VirusTotal : Retrohunt | PRÊT MAIS NON AUTORISÉ | `retrohunt_disabled_by_configuration` ; aucune soumission sans validation YARA + G2 |
| Shodan passif | BLOQUÉ PAR PRÉREQUIS EXTERNE | clé plan `oss` : 403 sur host/search (non rejoué ici) |
| Strings / PE / ELF (`static`) | OPÉRATIONNEL ET TESTÉ | worker attesté `autowork-static-v1` ; tests en-process avec vrais moteurs |
| capa 9.4.0 | OPÉRATIONNEL ET TESTÉ | idem + ruleset épinglé `2af9fbfc…` ; correctif du parseur (alias `attack`, adresse absente) |
| SMDA 4.5.0 | OPÉRATIONNEL ET TESTÉ | code features bornées + hash dans la preuve |
| FLOSS 3.1.1 | OPÉRATIONNEL ET TESTÉ | version attestée dans le conteneur ; extraction testée sur PE généré |
| MWCP 3.16.1 | PRÊT MAIS NON AUTORISÉ | installé et invoqué sur parser inoffensif de test ; `MWCP_PARSER_VERSIONS` vide ⇒ `parser_allowlist_empty` |
| YARA 4.5.0 (compilation / matching / corpus) | OPÉRATIONNEL ET TESTÉ | exécuté dans le worker ; vérifié aussi dans le conteneur durci |
| YARA — validation d'une règle réelle | NON VALIDÉ (honnête) | positifs réels indisponibles (download VT bloqué) ; import manuel `rule-corpus/imports` possible ; statut publié « CANDIDATE — NON VALIDÉE » |
| TShark/capinfos 4.0.17 / PCAP | OPÉRATIONNEL ET TESTÉ (fixture) | `-n` + allowlist de protocoles ; PCAP synthétique DNS/HTTP/TLS/TCP → IOC → flux → messages ; exécuté dans le conteneur durci |
| Modèle de protocole + diagramme D2 + ancrage avis | OPÉRATIONNEL ET TESTÉ (fixture) | W15 ; observé/hypothétique distingués |
| Ghidra 12.1.4 headless (service `ghidra`, profil compose) | OPÉRATIONNEL ET TESTÉ (fixture) | auto-test dans l'image + décompilation réelle d'un ELF synthétique depuis le backend (`return 0x2a`) |
| REA / `native.inspect` unifié | BLOQUÉ PAR PRÉREQUIS EXTERNE | aucun fournisseur/runner REA attesté (`provider_unavailable:none`) |
| Bwrap local (confinement FS mesuré) | BLOQUÉ PAR PRÉREQUIS EXTERNE | userns refusés par noyau/Docker ; remplacé par l'isolation conteneur |
| Éditorial : tableau IOC complet, figures (chronologie, chaîne d'enquête, graphe infra/pivots), médias archivés, notes ancrées, bandeau YARA, monospace | OPÉRATIONNEL ET TESTÉ | rendu réel Typst/D2/Poppler + contrôles bbox ; inspection visuelle des PNG par l'orchestrateur (fixtures) |

Les statuts « fixture » signifient : preuve sur données synthétiques inoffensives ; aucune analyse de malware réel n'a été validée.

## Activation (valeurs non secrètes à poser dans `.env`)
`LOCAL_ANALYSIS_ENABLED=true`, `ANALYSIS_WORKER_ENABLED=true`, `GHIDRA_ENABLED=true`, `COMPOSE_PROFILES=ghidra`
(`LOCAL_ANALYSIS_REQUIRE_FILESYSTEM_CONFINEMENT` vaut désormais `true` par défaut ; `ANALYSIS_WORKER_PROBE_TIMEOUT_SECONDS=8`).

## Validations de clôture
`make lint`, `make typecheck-backend`, `make test-backend` (3914 passés), `make test-integration` (324), `make test-workflow-full` (30),
frontend (289). Images : test d'image `ANALYSIS_WORKER_IMAGE_TEST=1 … tests/test_confined_analysis_image.py`, auto-test Ghidra
`docker compose --profile ghidra run --rm --no-deps --entrypoint python3 ghidra -m cti_app.ghidra_service.selftest`.
Les tests Typst réels exigent `TMPDIR` sous `$HOME` (snap).

## Limites restantes
- Pas d'invocation applicative bout-en-bout de `sample.analyze`/`tshark`/`yara` sur la **base réelle** (jobs écrivant en base de production) : prouvé par tests en-process + probe + exécution dans le conteneur, pas par un job réel.
- Débordement mineur : l'en-tête du tableau de provenance du graphe d'infrastructure frôle sa première ligne.
- Proxy VT (routes fichier/téléchargement) et plan Shodan : actions humaines externes.
