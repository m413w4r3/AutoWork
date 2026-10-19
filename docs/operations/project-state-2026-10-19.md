# État du projet — fin de session du 2026-10-19 (pour reprise)

Orchestrateur: session Claude Code `1ba16e4c-c310-4c4c-b8b1-eb80b81613b5`. Mandat maître:
`nimbalyst-local/reference/AutoWork_prompt_operateur_VT_run_reel.md`. Journal des lots: `nimbalyst-local/orchestration-2026-10-18.md`.
Journal de l'opérateur Claude: `nimbalyst-local/operator-journal-run12.md` (passes 1 à 5). **Rien n'est committé** (≈153 fichiers modifiés/nouveaux
depuis HEAD `47ae5be`); l'utilisateur n'a pas demandé de commit.

## 1. Vérifié par l'orchestrateur (dernière passe, machine peu chargée)
- `make lint` OK, `make typecheck-backend` OK (767 fichiers), `uv sync --locked` OK.
- Tests unitaires 4134 passés / 2 ignorés (image non construite, Ghidra absent localement); intégration PostgreSQL 329 passés;
  workflows 22 passés; frontend 289 passés. Les échecs Typst vus par moments viennent de `TMPDIR` mal posé (utiliser
  `TMPDIR=$HOME/audit-tmp`) ou d'une machine très chargée (timeout Typst 20 s); un échec d'intégration unique non expliqué a été vu une fois
  puis non reproduit.
- Stack redéployée (`make up`, sans reset, sans job actif) avec images reconstruites; `GET operator/context` répond (251 384 / 262 144 o).

## 2. Ce qui est prouvé en réel (stack de production)
- **Cause des 404 VT**: le proxy VT ajoute `&apikey=` brut ⇒ toute requête sans query string casse. Corrigé (sentinelle `x=1`, réglage
  `VIRUSTOTAL_PROXY_QUERY_SENTINEL_ENABLED`). Rapport fichier, `/download` (302 → `vtsamples.commondatastorage.googleapis.com`),
  `/download_url`, objets, relations, comportements fonctionnent via le proxy. PCAP: redirection vers `vtbehaviour.commondatastorage.googleapis.com`.
- **Téléchargement VT réel**: 4 échantillons (RuntimeBroker.dll 33 280 o `53f35e49…`, PsProxy.dll 6 144 o `3fd810a3…`, Blackwood.dll 8 487 424 o
  `76273382…`, archive ZIP 134 661 o `6e7d9b33…`) en `samples-quarantine`, SHA-256 vérifiés; réutilisés sans retéléchargement par les
  investigations suivantes du même Subject.
- **Moteurs confinés (analysis-worker)**: Ghidra headless (list_functions/imports/search; la décompilation `.NET` est `empty_decompilation`, attendu pour du CIL),
  FLOSS, `monodis` (CIL .NET), `file/strings/xxd/readelf/objdump/sha256sum`, TShark/capinfos, Suricata 7.0.10 (validate/replay), YARA.
- **Schéma live corrigé** (accord utilisateur): `ck_vt_observation_capability` accepte `behaviour_reports`; sauvegarde
  `~/audit-tmp/ddl/virustotal_observations.pre-ddl.dump`; SQL/rollback dans `docs/operations/vt-observation-capability-drift.md`.
- **Mode opérateur local (`operator_led`)**: un client Claude (identité `operator_client`, jeton distinct) pilote l'investigation par l'API; le Bridge
  est un conseiller borné (`advisor.consult`, G2 autorise le fournisseur `bridge`). Investigation 12 `5f66ebfb-1653-45e6-83cd-a29af6f87868` (run 12
  `ec7a6807-ec96-4012-864a-6ca033faa951`, Subject `d22822d4-27ce-47d2-9cc5-f84cf312986a`, édition `11f62c2b-5174-42ab-bf9f-9f4a524741e9`).
- **Contenu technique lu (analyse statique uniquement)**: PsProxy.dll = hôte PowerShell .NET (`System.Management.Automation`, `CreateRunspace`…);
  Blackwood.dll = chargeur PE manuel (`DynamicDllLoader`/`MEMORYMODULE`, `RC4`, `ConfigReader`, `GetValueFromBase64` lit des paires clé=valeur,
  mutex nommé dans `Init.InitializeNewDomain`); RuntimeBroker.dll = `InitializeNewDomain` (méthode 3 de `RuntimeBroker.Init`, ~207 méthodes
  obfusquées, tas de chaînes utilisateur vide); ZIP = projet Visual Studio `DubaiAirport_Carrers_IT_Test`; 91.107.156.29 hébergé chez Hetzner.
- **Règles YARA**: 3 candidats testés explicitement (statut `real_partial`, 0 faux positif sur 12 DLL .NET Mono réelles bénignes importées
  comme goodware); sensibilité non mesurée (aucun positif confirmé: qualification humaine requise); `rule.revise` non justifiée.
- **Avis (brouillon, jamais approuvé)**: `opinion_id d9935324-2ccc-5067-9dc9-17efc0637823`, **version 14**, sha256
  `8b182ca4f580982bb97f57730032e61585f084ac1ff645cd33ef65a86e6b3484`; aperçu PDF 19 pages (`~/audit-tmp/pdf12/opinion-v14.pdf`, sha256
  `de07198ac6ae9384a575fda97a849cb65840fb9a5e2f9db85d9375c21b218ca7`): références propres, chronologie, section « Avis de l'analyste »,
  tableau IOC compact, trois vues du modèle de protocole **documenté, non observé** avec légende. Modèle: blob
  `e3b0d242-43ca-4b17-a406-b0a97a731acd`, rendu `fef52fde-e1f9-506f-977d-35426fca3c00`.

## 3. Ce qui n'est PAS prouvé / reste ouvert
- Aucune conclusion de comportement (exécution, C2 GitHub, tunnel Chisel, configuration, déchiffrement de la charge, attribution): analyse statique seulement.
- Le trafic des 4 PCAP VT (RuntimeBroker 7 807 o → 52 paquets; Blackwood 1 219 o → 8; ZIP 2 181 o → 16; PsProxy 24 o → 0) n'a pas encore été lu
  par l'opérateur depuis le correctif P2 (la cause `--only-protocols` TShark 4.0.17 est corrigée et déployée); `network.suricata_replay`, flux et
  `network.protocol.hypothesize|verify` restent à faire. Le replay Suricata d'une capture vide (PsProxy) est corrigé mais non revérifié en réel.
- capa: l'échec observé venait d'un résultat échoué en cache (clé identique); correctif `capa_runner_revision = confined-r2` déployé mais
  **non revérifié par un nouveau `sample.analyze` réel** (un direct run du worker déployé a réussi: 37 capacités sur RuntimeBroker). ZIP = non pris en charge.
- `monodis cil` sur RuntimeBroker: `monodis_method_body_missing` (corps absent pour la méthode visée) — à retenter sur d'autres RID.
- `advisor.consult yara_draft`: le lot Q (brief serveur compact + `advisor_guidance`) est déployé mais **jamais exercé en réel**.
- `finding.revise` crée un nouvel identifiant à chaque révision (comportement de la plateforme): citer toujours le dernier identifiant.
- Anciennes notes d'avis sans accents (passe 3) peuvent subsister à côté des nouvelles.
- G3 (approbation de l'avis), publication, repin/composition d'édition, rendu final de la release et inspection du PDF final: **non faits** (décisions humaines;
  le PDF d'avis v14 est un aperçu « BROUILLON NON APPROUVÉ »). Qualification des 4 échantillons en positifs confirmés: décision humaine.
- REA: aucun fournisseur attesté (`native.inspect` indisponible), Ghidra le remplace pour le natif; MWCP: liste de parseurs vide; Shodan: plan `oss`
  (host 403 sur la plupart des IP); Retrohunt désactivé; analyse de code managé: Ghidra ne décompile pas le CIL (utiliser `monodis`).
- Esthétique du PDF: titre « IOC » proche de la synthèse (espacement), comparaison page à page avec `docs/references/CN2609_typ` à refaire sur
  le PDF final réel.

## 4. Défauts de plateforme découverts et corrigés pendant cette session (résumé)
Transport VT (sentinelle, redirections signées bornées, v2 optionnel, redaction `apikey`); acquisition/association (`ALREADY_DOCUMENTED`, réutilisation
subject-level, cache de refus scopé); sample.analyze/rules/local.run_command via le worker confiné (gating bwrap supprimé); `finish` gate et visibilité des
échantillons pour l'agent; Bridge pacing/jitter et reprise; mode `operator_led` (invocation durable, contexte borné par priorité avec troncature,
sorties lisibles, observations paginées); PCAP VT → captures inspectables; TShark `--only-protocols`; YARA `engine_crashed`; rendu PDF (avis, références,
chronologie, tableau IOC compact, figures protocole documentaire, normalisation d'affichage sans toucher le texte hashé); compose/.env (jetons opérateur).

## 5. Configuration à connaître (sans secret)
- `.env` (sauvegardes dans `/tmp/.env.pre-*`): `VIRUSTOTAL_DOWNLOAD_ALLOWED_HOSTS=vtsamples…,vtbehaviour…`, `VIRUSTOTAL_BEHAVIOUR_REPORTS_ENABLED=true`,
  `VIRUSTOTAL_BEHAVIOUR_PCAP_ENABLED=true`, `OPERATOR_API_TOKEN` et `OPERATOR_CLIENT_API_TOKEN` générés (distincts; le frontend reçoit le jeton humain,
  jamais le jeton client). Scripts locaux: `~/.cache/audit/api.sh` (identité humaine) et `~/.cache/audit/opapi.sh` (client opérateur).
- Une clé de 64 caractères hexadécimaux (peut-être la clé VT) est apparue une fois dans une sortie d'outil (erreur du proxy): régénération conseillée.
- Corpus goodware importé par investigation via `~/audit-tmp/run9/import_gw.sh` (12 DLL Mono bénignes, `~/audit-tmp/run9/gw`).

## 6. Pour reprendre (ordre recommandé)
1. `git status`; décider du commit (aucun fait). Relire ce document et `docs/operations/operator-playbook-medium.md`.
2. Vérifier la stack (`docker compose ps`), `GET operator-status` de l'investigation 12 (`opapi.sh`), puis lancer une session opérateur
   (Claude Sonnet 5.5 effort moyen) avec le playbook: relire status+context → consulter l'advisor quand `checkpoint_due` → inspecter les 4 PCAP
   (`network.capture.inspect`, `network.suricata_replay`) → rejouer `sample.analyze` (capa) → `advisor.consult yara_draft` → `rule.propose/test` →
   `opinion.revise` (réviser les blocs, joindre `protocol_figures`).
3. Régénérer l'aperçu PDF de l'avis et l'inspecter (`pdftoppm` + `pdftotext`) contre `docs/references/CN2609_typ`.
4. Décisions humaines: qualification des échantillons, G3, publication, repin d'édition, rendu final de la release, inspection du PDF final.
5. Ne pas lancer `make clean`/`down -v`/reset; jamais de DDL live sans accord explicite; ne pas interrompre un tour actif.
