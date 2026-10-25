# Plan de redémarrage d'une investigation réelle

Statut : **PLAN PRÉPARÉ — AUCUNE ACTION OPÉRATIONNELLE AUTORISÉE.** Aucun
accès live PostgreSQL/MinIO, aucun appel VT/Bridge/Shodan, aucun
téléchargement ni lancement n'a été effectué. Preuve de reconnaissance :
[`recon/RUN_STATE.md`](recon/RUN_STATE.md) (SHA-256
`7a71689e65cf6198c16550d05a59ca03dfb97a877d00b268dbd4cb0ee28b01ed`).

## 1. Run de référence

- **Dernier run réel documenté :** Run 12, 19–20 octobre 2026, référence de
  départ `main@77a276e` (abrégée), puis clôture éditoriale du 22 octobre 2026
  sur une référence différente `main@2317440…`.
- **Sujet :** Blinder Tunnel — campagne d'espionnage contre les
  infrastructures critiques irakiennes via faux recrutement.
  `subject_id = d22822d4-27ce-47d2-9cc5-f84cf312986a`.
- **Investigation 12 :** `5f66ebfb-1653-45e6-83cd-a29af6f87868`.
- **Run 12 :** `ec7a6807-ec96-4012-864a-6ca033faa951`.
- **Édition :** `11f62c2b-5174-42ab-bf9f-9f4a524741e9` (TLP CLEAR d'après le
  journal initial).

Ces identifiants proviennent de documents datés conservés. Le rapport du
20 octobre ne répète pas les UUID et ne prouve pas une jointure ligne à ligne
avec le run du 19 octobre : **association plausible, non vérifiée en base**.

## 2. Lignée de synthèse à figer

- **UNKNOWN :** `synthesis_artifact_id`, version, SHA-256 canonique et blob de
  la synthèse exacte de Run 12. Le journal de clôture cite une opinion v61
  (SHA-256 `82a1c86218ccb7759669a20f3001bd1c45f5683cec5dabbb1d7f34911428ee49`)
  mais pas l'artefact de synthèse.
- **UNKNOWN :** si l'opinion v61 descend de l'opinion v14 du 19 octobre.
- **Contrainte vérifiée :** une investigation distincte exige un
  `production_run_id` distinct, et la contrainte unique
  `analyst_investigations.production_run_id` empêche deux investigations sur
  le même run (`models/production.py:336-340` ; `api/investigation.py:917-924`).
  Le run doit posséder un artefact de synthèse `VERIFIED`, du bon sujet, avec
  un `canonical_sha256` correspondant au contenu gelé approuvé
  (`api/investigation.py:831-848,2641-2654`).
- **UNKNOWN bloquant :** la capacité actuelle à faire porter à un nouveau run
  un artefact de synthèse identique sans recalcul ni changement de lignée.
  Ne pas supposer que la réutilisation inter-run est supportée.

## 3. Preuves et échantillons déjà acquis

- Quatre échantillons VT documentés dans `samples-quarantine`, SHA-256
  vérifiés, réutilisés sans nouveau téléchargement :
  `RuntimeBroker.dll` (33 280 octets, préfixe `53f35e49…`), `PsProxy.dll`
  (6 144, `3fd810a3…`), `Blackwood.dll` (8 487 424, `76273382…`), archive ZIP
  (134 661, `6e7d9b33…`).
- **UNKNOWN :** correspondance complète nom → SHA-256, IDs de samples et de
  blobs, associations par investigation, historique G3, disponibilité MinIO.
- Artefacts de production initiale documentés : extraction
  `495fc542-74f8-4065-8499-094c6d594fd5` v1 (préfixe `d08dba02…`), synthèse
  `64baa901-4b35-4ffd-97c5-3ed1a3ec74eb` v1 (SHA-256
  `8eb9cbe0fade4cffe00fb0f5f645a5f2dd074ce431e07c16a0e487a205bdd618`), V5
  `114d0130-19ec-4761-8576-967ee6882648` v1 (SHA-256
  `5cf74aa7feda9fbbc1b0ea1cf9f9913032c266b18f966f89ae55add6a75367c8`).
  Ces artefacts ne sont pas établis comme la lignée exacte de Run 12.
- Quatre PCAP comportementaux documentés (RuntimeBroker 7 807 octets/52
  paquets, Blackwood 1 219/8, ZIP 2 181/16, PsProxy 24/0) ; SHA/refs blobs
  **UNKNOWN**.
- Le décompte de 1 290 observations de preuve est global à la base, pas
  propre à Investigation 12.

## 4. Investigations antérieures

| Investigation | État documenté | Limite |
|---|---|---|
| `f0685708-e887-40f2-8a32-0075c083aa4f` | run initial `a6d09c1d…`, terminée `agent_finished` v23, 14 tours, 11 findings, opinion v3 | état actuel UNKNOWN ; G2 `do_not_submit=true` avait bloqué Bridge |
| `5f66ebfb-1653-45e6-83cd-a29af6f87868` | Investigation 12, `running` au 19 octobre | état final et lignée UNKNOWN |
| autres | — | toute investigation supplémentaire sur le sujet est UNKNOWN |

Aucune de ces investigations ne doit être supprimée, réinitialisée,
réestampillée ou réécrite.

## 5. Préconditions avant toute action

1. **Lecture canonique autorisée** : obtenir les runs, artefacts de synthèse,
   investigations et statuts exacts du sujet, ainsi que la version Alembic et
   le schéma applicatif. La requête read-only proposée est dans
   `recon/RUN_STATE.md` §7 ; elle n'a pas été exécutée.
2. **Lignée gelée approuvée** : identifier explicitement le
   `(production_run_id, synthesis_artifact_id, version, canonical_sha256,
   blob_id)` du contenu gelé, et vérifier que le run est distinct et possède
   cet artefact `VERIFIED`. Si le modèle actuel ne peut pas représenter cette
   lignée, s'arrêter et demander une décision de conception.
3. **Réconciliation** : lire les jobs, invocations, tours et ModelRuns
   `RUNNING`/`NEEDS_REVIEW`/ambigus, et les réconcilier par la route CAS
   humaine (`api/investigation.py:3847-3911`) avant toute reprise ou nouvelle
   action externe.
4. **Preuves** : énumérer les hashes, blobs de quarantaine, observations,
   tentatives d'acquisition et historiques G3 ; vérifier les octets par le
   SHA canonique avant réutilisation. Ne jamais supprimer, écraser ou
   retélécharger un échantillon existant.
5. **Politique et capacités** : lire la TLP/disclosure de la synthèse gelée,
   l'enveloppe G2 courante (ou la politique `OSINT_OPEN` activée), les
   budgets, l'attestation worker/confinement, les versions d'outils et
   l'identité opérateur authentifiée.
6. **Stockage** : prouver que les refs blobs enregistrées se résolvent et que
   le contenu reproduit le SHA. S3 reste bloqué jusqu'à ses propres gates.
7. **Observateur indépendant** : nommer un observateur humain en lecture seule,
   avec un journal append-only séparé (horodatage/checkpoint, ID de requête ou
   de job, version/hash source, défaut observé, référence de preuve). Il ne
   corrige pas silencieusement, ne réessaie pas et n'autorise aucun effet
   externe. Toute correction est une action séparée, revue et autorisée.
8. **Autorisation** : obtenir une autorisation explicite pour l'action exacte,
   le propriétaire, la lignée source, le périmètre externe, les décisions
   G2/G3, les budgets et la clé d'idempotence.

## 6. Investigation nouvelle

- **Objet :** nouvelle investigation distincte sur le même sujet, depuis la
  synthèse gelée approuvée.
- **Mode :** `OSINT_OPEN` si la politique est activée et applicable au sujet ;
  sinon `RESTRICTED` avec G2 explicite.
- **Interdits :** supprimer ou modifier une analyse antérieure ; réutiliser le
  même `production_run_id` ; télécharger à nouveau un échantillon déjà
  quarantiné ; promouvoir automatiquement un `candidate` en `confirmed`.
- **Sortie attendue :** une investigation nouvelle, traçable, avec ses
  associations `candidate`, ses preuves, ses coûts et ses décisions ; aucune
  conclusion de confiance n'est créée par la préparation.

## 7. Journal d'exécution (à remplir pendant un run autorisé)

| Élément | Valeur |
|---|---|
| Nouveau `production_run_id` / numéro / sujet | `UNKNOWN — action non autorisée` |
| Artefact de synthèse du nouveau run / version / SHA-256 | `UNKNOWN — lecture canonique requise` |
| Nouvelle investigation / job / version attendue / clé d'idempotence | `UNKNOWN — action non autorisée` |
| Associations réutilisées / SHA vérifiés / blobs / provenance | `UNKNOWN — lecture canonique requise` |
| Checkpoints observateur / références du journal de défauts | `UNKNOWN — futur run` |
| Critères d'arrêt | lignée incohérente, blob absent/corrompu, `needs_review` non résolu, version CAS obsolète, capacité indisponible, refus de politique, effet de bord non autorisé |

## 8. Non-actions explicites

- Aucun reset, suppression, écrasement ou restampillage d'investigation,
  finding, observation, artefact, opinion, job, sample ou blob antérieur.
- Aucun téléchargement ou réacquisition d'un échantillon déjà documenté ;
  aucune nouvelle acquisition, rescan, Retrohunt, exécution de malware,
  contact C2 ou upload.
- Aucun appel Bridge/VT/Shodan sans autorisation séparée et sans les contrôles
  G2/disclosure courants.
- Aucune approbation G2/G3, extension/révocation d'enveloppe, approbation
  d'opinion, publication, release, repin, migration ou cutover S3 dans ce
  plan.

## 9. Verdict

```text
REAL_RUN_PREFLIGHT_READY|PARTIAL|BLOCKED : PARTIAL
REAL_INVESTIGATION_NOT_STARTED           : CONFIRMED
```

Le plan est prêt et les inconnues sont explicites. Le passage à READY exige
les lectures canoniques autorisées et la preuve d'une lignée de synthèse gelée
portée par un run distinct. Aucune de ces lectures n'a été effectuée ici.
