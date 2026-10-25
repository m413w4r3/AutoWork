# Prévol VT, Bridge et analyse locale

Statut : **PRÉVOL STATIQUE — aucun appel fournisseur, aucune connexion
PostgreSQL/MinIO, aucun outil local exécuté.** Point d'évaluation :
`505c1cd2c525744bee5d768cccc1a830abce2d2b`. Rapport détaillé :
[`recon/RUNTIME_READINESS.md`](recon/RUNTIME_READINESS.md) (SHA-256
`8d45480896e27945087c99786cd995dce6670831f5ee05edae6833fee4220f47`).

## 1. Règle de lecture

`READY` signifie **contrat de code borné et testé hors ligne**, jamais une
connectivité ou une configuration de déploiement vérifiée. `PARTIAL` signifie
que le chemin existe mais qu'une condition runtime, de politique, de preuve ou
de complétude reste non résolue. `BLOCKED` signifie que le défaut versionné
désactive la capacité. `UNKNOWN` n'est jamais un PASS.

**Aucune capacité live n'est vérifiée READY.** Les 46 lignes du rapport se
répartissent en 21 READY au niveau contrat, 13 PARTIAL, 1 BLOCKED, 11 UNKNOWN
et 0 READY live.

## 2. VirusTotal

| Capacité | Classification | Preuve / limite |
|---|---|---|
| File report, relationships, intelligence search, behaviours | READY contrat ; live UNKNOWN ; défaut BLOCKED | Routes et pagination dans `infrastructure/virustotal.py:943-964,1020-1036,1090-1147,1557-1606` ; opt-ins par capacité à `config.py:215-229`. |
| Transport proxy et routes | READY contrat ; live UNKNOWN ; défaut BLOCKED | Client sans proxy ambiant ni redirect automatique (`virustotal.py:122-158`) ; activation conditionnée à URL/base et opt-in (`:1464-1495`). Défaut sans proxy (`config.py:206-222`). |
| Fallbacks directs V2/V3 | PARTIAL ; live UNKNOWN ; défaut BLOCKED | Seul `file_report` a des fallbacks, chacun avec son propre flag (`virustotal.py:1557-1611` ; `config.py:223-228`). |
| Téléchargement et vérification des octets | READY contrat ; live UNKNOWN ; défaut BLOCKED | `/files/{hash}/download` puis `download_url`, hash et taille vérifiés en streaming (`virustotal.py:440-458,492-545`) ; téléchargement désactivé par défaut (`config.py:237-252`). |
| Redirections signées | READY contrat ; live UNKNOWN | Redirections manuelles, client CDN sans credential, HTTPS/host/port exacts, 3 sauts max (`virustotal.py:122-133,1940-1968,2041-2048,2069-2075`). Tests de transport déterministes, pas de DNS/CDN réel. |
| Quota et entitlement du compte | UNKNOWN | Aucun compte inspecté ; 429 n'est pas une preuve de quota. Budgets G2/VT par défaut à zéro (`config.py:385-392`). |
| Archivage des réponses brutes | READY contrat ; stockage UNKNOWN | Pages brutes archivées avec blob/SHA canonique ; erreurs non promues (`tests/test_investigation_vt_tools.py`). Le stockage live n'est pas vérifié. |
| Nouveau téléchargement vs réutilisation locale | READY contrat ; stockage UNKNOWN | Réutilisation après vérification du SHA-256 et des octets ; zéro unité VT/download en réutilisation (`investigation_acquisition.py:201-288`). |
| Pagination bornée | READY contrat ; live UNKNOWN | Curseur, nombre de pages et résultat non exhaustif préservés (`virustotal.py:1164-1222`). |
| Upload/soumission VT | BLOCKED pour ce périmètre | Aucune soumission autorisée ; ne pas déduire un chemin actif d'un champ de capability. |

## 3. Bridge

| Capacité | Classification | Preuve / limite |
|---|---|---|
| Sélection du transport | READY contrat ; live UNKNOWN ; défaut non configuré | `POST /responses` et `GET /responses/{id}` (`integrations/models.py:90-110`) ; composition seulement si endpoint/key/route (`investigation_runtime_ports.py:831-865`). Le loopback par défaut sans clé ne compte pas comme configuré. |
| Capabilities et archive de conversation | PARTIAL ; live UNKNOWN | Routes `/bridge/capabilities` et DELETE conversation non appelées (`models.py:541-582`). Santé du service et de l'extension UNKNOWN. |
| Types de consultation | READY contrat ; live UNKNOWN | `plan_investigation`, `pivot_strategy`, `yara_draft`, `review_text`, `draft_text`, `suricata_draft`, `figure_plan` (`investigation_advisor_tools.py:306-353`). |
| Dossier et sélection des preuves | READY contrat ; stockage UNKNOWN | Refs explicites et observations liées ; manifeste, SHA, octets et refs transmises (`investigation_advisor_tools.py:413-455,684-728`). |
| Références transmises | READY contrat ; live UNKNOWN | Refs calculées avant soumission et enregistrées en provenance (`:715-728,1219-1264`). |
| Archive du prompt avant envoi | READY contrat ; stockage UNKNOWN | Prompt et dossier persistés dans `advisor-prompts` avant l'appel ; échec d'archive = refus (`:1020-1047,1106-1124`). |
| Coût, quota et budget de contexte | PARTIAL ; coût live UNKNOWN | Budget mesuré avant transport (`:728-760`) ; la dépense réelle du fournisseur n'est pas déductible d'une estimation. |
| Statut et reprise d'un tour | PARTIAL ; live UNKNOWN | Statut durable et récupération par ID (`api/investigation.py:1614-1754` ; `bridge_investigator.py:583-665`). Pas de preuve d'un job Bridge récupérable aujourd'hui. |
| Sortie partielle/ambiguë | READY contrat ; live UNKNOWN | Parser et statuts préservent partiel/ambigu ; pas de retry aveugle (`investigation_advisor_tools.py:3585-3795`). Tests fake uniquement. |
| Réconciliation | READY contrat ; action humaine requise | Route de réconciliation CAS avec acteur vérifié (`api/investigation.py:3847-3911`). |
| Appel Bridge réel réussi | UNKNOWN | Aucun test live ; Run12 est une observation historique, pas une preuve actuelle. |

## 4. Analyse locale

| Capacité | Classification | Preuve / limite |
|---|---|---|
| `sample.analyze` | READY contrat ; runtime/stockage UNKNOWN ; défaut BLOCKED | Échantillon quarantiné uniquement, observations `BINARY_ANALYSIS`/`BINARY_STRING` (`investigation_acquisition_tools.py:202-255,650-698`). Worker désactivé par défaut (`config.py:318-328`). |
| `local.run_command` | READY contrat ; runtime UNKNOWN ; défaut BLOCKED | Catalogue attesté, confinement filesystem strict, sortie enregistrée (`investigation_local_tools.py:270-328,491-561`). |
| Ghidra | PARTIAL ; runtime UNKNOWN ; défaut BLOCKED | Cinq outils read-only ; contrat `/v1/probe`, versions Ghidra/Java (`ghidra_mcp.py:71-185,330-366`). Image non inspectée. |
| capa | PARTIAL ; runtime UNKNOWN ; défaut BLOCKED | Famille disponible seulement si moteur attesté et versions/ruleset exacts (`confined_analysis_dispatch.py:462-475,555-575`). |
| YARA | PARTIAL ; runtime/règles UNKNOWN ; défaut BLOCKED | Catalogue borné et famille attestée YARA 4.5.0 ; fixture synthétique seulement ; Run12 `ONE_RULE_NOT_VALIDATED` est historique. |
| PE/.NET | PARTIAL ; runtime UNKNOWN ; défaut BLOCKED | `objdump`, `monodis`, ILSpy attesté (`local_analysis.py:435-546` ; `confined_analysis_dispatch.py:485-497`). |
| PCAP/PCAPNG | PARTIAL ; runtime/capture UNKNOWN ; défaut BLOCKED | `network.capture.inspect` sur capture quarantinée ; tshark/Suricata pinnés (`investigation_network_tools.py:50-127`). |
| Confinement et attestation | PARTIAL ; attestation actuelle UNKNOWN ; défaut BLOCKED | UID 10001, no-new-privileges, zéro capability effective, root read-only (`confined_analysis_dispatch.py:306-412`). Le probe n'a pas été lancé. |
| Versions installées | PARTIAL ; UNKNOWN | Pins versionnés dans `config.py:329-353,385-425` ; aucune version installée vérifiée. |
| Sorties locales enregistrées | READY contrat ; stockage UNKNOWN | Blob/SHA canonique et observations ; le stockage live n'est pas vérifié. |
| Image Ghidra réelle | UNKNOWN | Test réel optionnel non exécuté (`tests/test_ghidra_service_real.py:12-29`). |

## 5. Prérequis et inconnues de déploiement

- VT : URL proxy, clé, flags de routes et fallbacks **UNKNOWN** ; défauts
  versionnés **BLOCKED** (`config.py:198-252`).
- Bridge : URL/clé/route et santé UI **UNKNOWN** ; défaut non configuré
  (`config.py:105-107,140-145` ; `investigation_runtime_ports.py:848-865`).
- Politique par cas : G2, TLP, `external_llm_allowed`, `do_not_submit`,
  révocation et budgets restants **UNKNOWN** sans lecture canonique.
- PostgreSQL, MinIO et stockage de preuves : **UNKNOWN**.
- Worker d'analyse, sandbox, binaires, versions et hashes de ruleset :
  **UNKNOWN** ; désactivés par défaut.
- Ghidra : hôte/version/contrat **UNKNOWN** ; défaut désactivé.
- Un écart documentaire existe entre les variables d'environnement décrites
  dans `investigation_operations.md:118-120` et le contrat actuel
  `/v1/probe` de `ghidra_mcp.py:136-185`.

## 6. Checklist de prévol pour un futur run autorisé

1. Lire `GET /api/investigations/{id}/operator-status` : outils disponibles,
   raisons d'indisponibilité, G2/révocation, budgets, tour Bridge en attente,
   statuts d'invocation et coût projeté. Route :
   `api/investigation.py:1614-1754`.
2. Lire le contexte opérateur et son SHA de version :
   `GET /api/investigations/{id}/operator/context?view=summary`.
   Détecter toute action planifiée obsolète.
3. Lire l'enveloppe G2 courante :
   `GET /api/investigations/{id}/envelope`. S'arrêter si absente, révoquée,
   obsolète, hors budget ou si la divulgation externe n'est pas autorisée.
4. Obtenir les attestations runtime par le canal opérateur/déploiement
   approuvé : worker joignable/confinné/dépendances/profil, versions et hashes
   de ruleset, familles actives, contrat Ghidra, routes VT prêtes, endpoint et
   route Bridge. Aucune route publique d'inventaire complet n'a été trouvée ;
   `describe_tool_inventory` existe (`investigation_runtime_ports.py:868-963`).
5. Ne contacter Bridge (`/bridge/capabilities`) qu'après autorisation explicite
   de contact externe.
6. Ne tester un quota VT qu'avec l'opération de cas explicitement autorisée,
   jamais par un probe sur un échantillon.
7. Pour l'analyse locale, exiger un échantillon déjà associé et quarantiné,
   l'outil disponible dans `operator-status`, et conserver les refs
   d'observation canoniques ; pour PCAP, exiger la capture quarantinée et son
   SHA/taille/format.

## 7. Verdicts de cette prévol

```text
LOCAL_ANALYSIS_READY|PARTIAL|BLOCKED       : PARTIAL
VT_BRIDGE_LIVE_CONNECTIVITY_VERIFIED      : NON — UNKNOWN, aucun appel autorisé
VT_BRIDGE_CONTRACT_EVIDENCE               : READY (source/tests hors ligne)
RUNTIME_DEFAULTS                          : BLOCKED pour VT/Bridge/local worker
REAL_RUN_PREFLIGHT_READY|PARTIAL|BLOCKED  : PARTIAL — voir RUN_RESTART_PLAN.md
```

`LOCAL_ANALYSIS_READY` est PARTIAL : les contrats et les tests à fixtures
inertes existent, mais le worker, le confinement attesté, les versions
installées et le stockage canonique ne sont pas vérifiés en live. Aucun appel
fournisseur n'a été effectué pour produire ce document.
