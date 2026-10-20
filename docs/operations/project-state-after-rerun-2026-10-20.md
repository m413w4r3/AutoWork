# AutoWork — état après le re-run réel du run 12 (2026-10-19 → 2026-10-20)

Base de départ : `main@77a276e`. Ce document consigne ce que le re-run réel a
démontré, les défauts trouvés et corrigés, les leçons à ne pas reproduire, et ce
qui reste ouvert. Les journaux détaillés (tickets RUN12-CORR-001…031, checkpoints
CP-01…CP-08, QA PDF) sont dans `nimbalyst-local/run12-*.md` (ignoré par git,
copie locale uniquement). Rien n'est publié ni approuvé : G3, publication et
repin restent des décisions humaines.

## Opérations live réalisées
- DDL additive C2 (`investigation_experiment_heads`, `investigation_experiments`,
  index, fonction et trigger append-only) appliquée une fois, après sauvegarde
  `pg_dump` vérifiée par `pg_restore --list`. Le marqueur Alembic live reste
  `0001_baseline` : le dépôt n'autorise que la baseline mutable. Une révision
  `0002` a été prototypée puis retirée (tests d'architecture). Les autres
  installations déjà estampillées `0001` ne reçoivent donc pas ces tables
  automatiquement : décision à prendre.
- 5 redéploiements `make up` sans reset. Aucun job ni invocation actif à chaque fois.
- Aucune action G2/G3, publication, repin, upload, rescan, Retrohunt, contact C2
  ni exécution d'échantillon.

## Défauts corrigés (code + tests + rejeu live)
| Ticket | Défaut | Rejeu live |
|---|---|---|
| 001 | Scope opérateur refusait sections, `/pivots`, `/experiments` | PASS |
| 002/012 | Extraction ILSpy (nom court du type), parseur monodis, RuntimeBroker | PASS |
| 003/018 | Heuristique d'obfuscation (3 passes) | PASS (3e) |
| 004 | Compteur DNS doublé par paquet | PASS |
| 005 | `experiment.record` sans registre d'hypothèses → `hypothesis.declare` | PASS |
| 006 | Association de capture refusait les PCAP sandbox VirusTotal | PASS |
| 007/014/017 | Bridge : rejets non diagnostiqués, 500 `mappingproxy`, refs invalides | PASS |
| 008 | Liste des enquêtes en 500 (états legacy) | PASS |
| 009/010 | Preview PDF en 500 sur observations VT > 128 KiB ; pages brutes typées VT | PASS |
| 011 | `/pivots` en 500 (validation stricte des enums) | PASS |
| 013 | Texte des claims inaccessible à l'UI | PASS |
| 015/021 | Bruit infrastructure privée/bénigne dans la frontière (`list-v2`) | PASS |
| 016 | Cache périmé après redéploiement (clé sans schéma/attestation worker) | PASS |
| 019/026 | Trace de recommandation Bridge ; `operator-status` en 500 (liste vs tuple) | PASS |
| 020 | `/sample-comparisons` en 500 (blob 70 Mo) | PASS |
| 023 | Section « IOC originaux » absente | PASS |
| 024 | Pas de remplacement entre expériences (`supersedes_experiment_ids`) | PASS |
| 025/028 | Sortie persistée plafonnée à 4 000 car. ; chunks `xxd` masqués | PASS |

## Ouvert
- **CORR-031 (P2)** : figures de protocole du PDF v27 — niveau de preuve répété par
  nœud, vues format de message et infrastructure illisibles, murs de hashes en annexe.
- **CORR-030 (P3)** : le playbook donne un mauvais schéma pour `network.protocol.render`
  (réel : `schema_version "1"`, `model_blob_id`).
- **CORR-027** : nettoyage de `args_hint` non rejouable sans un nouvel appel Bridge.
- Les strings/ressources .NET n'alimentent pas la frontière de pivots.
- Audit critique indépendant en lecture seule : non fait.
- Alembic : voir plus haut.
- Couverture des claims : 16/22 majeures avec verdict référencé (4 supported,
  10 qualified, 2 inconclusive, 0 refuted) ; 6 impossibilités justifiées.
  Les 4 PCAP ne contiennent que du DNS de fond : protocole C2 `not_testable`.
- Avis : brouillon v27, jamais approuvé.

## Leçons — erreurs à ne pas reproduire
1. **Un test vert ne prouve pas le live.** Quatre correctifs ont passé leurs tests
   puis échoué en production : l'exception d'identité visait le mode `local` alors
   que le provider live est `authenticated` ; un seuil d'obfuscation calibré sur une
   fixture ; des figures déjà persistées dans l'ancien format. Toujours rejouer sur
   les artefacts réels avec de nouvelles invocations.
2. **Les clés de réutilisation/idempotence doivent inclure schéma de résultat et
   attestation du worker**, sinon un redéploiement sert des sorties périmées
   (CORR-016). Les outils externes payants (VT, Shodan, Bridge) gardent leurs clés.
3. **Tout contrat strict relu depuis du JSON** doit convertir listes en tuples à la
   construction ; ne pas desserrer la validation globale (CORR-026, 011).
4. **Les lectures non bornées de blobs** doivent omettre explicitement (ID, taille,
   raison) et jamais renvoyer 500 (CORR-009, 020).
5. **Parallélisme de correcteurs** : fichiers disjoints, et relire un fichier partagé
   (`api/investigation.py`, `operator_tool_invocation.py`) juste avant édition. Plusieurs
   régressions sont venues de changements concurrents ; lancer les suites complètes
   après chaque vague, pas seulement les tests ciblés.
6. **Environnement de test** : `unset CONDA_PREFIX` ; Typst réel exige un `TMPDIR`
   sous `$HOME` (le snap ne lit pas `/tmp`). Sans cela, 29 tests « real_typst »
   échouent à tort.
7. **Une sous-session peut bloquer sur un widget invisible** : interdire les widgets
   dans les mandats ; si bloquée, ouvrir une session de reprise plutôt qu'attendre.
8. **Opérateurs jetables** : nouvelle session par vague, briefing pointant vers les
   journaux, checkpoint après chaque correctif rejoué (limites de session).
9. **Ne pas exécuter `opinion.revise` ni rendre une figure en comptant sur une
   régénération implicite** : les figures du brouillon sont figées à leur création ;
   le re-rendu passe par la procédure du playbook.
10. **Qualifier avant de ticketer** : capture vide, 403 Shodan OSS, sensibilité YARA
    non calculable, refus de G2 ne sont pas des bugs.
