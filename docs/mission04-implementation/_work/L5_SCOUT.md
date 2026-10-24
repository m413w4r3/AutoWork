# Mission 04 — Scout L5 (précondition, lecture seule)

## Verdict

**NO_GO pour le lot complet en TEST-ONLY dans l’état inspecté.** L’oracle de
complétude canonique → PDF est réalisable sans changement de production ni de
template. En revanche, le `pdf_lineage` actuel n’expose pas `reviewer_id` ni
`approver_id`; les ajouter ou décider que la provenance V6 suffit dépasse un
lot strictement test-only et doit attendre la décision éditoriale demandée.
Le bundle de polices local requis pour compiler n’est pas présent non plus.

Le sous-lot « chronologie publiée → PDF, V5 inchangé, opinion approuvée exacte »
peut rester test-only. Si « lineage reviewer/approver » signifie uniquement la
provenance canonique V6 et les métadonnées de l’artefact, cette partie est déjà
stockée et peut être vérifiée par test. Si elle signifie le `pdf_lineage`
transmis au rendu/PDF, un changement de production est nécessaire.

## Base inspectée et limites

- HEAD réellement inspectée : `df9e091076cbfd3147690f184c5562d626d8ae66`.
- Branche réellement montée : `mission04/optional-scouts`.
- Le SHA demandé `df9e091007c2cbf3e5d8b2b4a0d9e2b6d8b0f2b0` est absent localement et
  la branche demandée `mission04/implementation` n’est pas la branche montée.
  Les constats de code ci-dessous portent donc sur le HEAD présent, pas sur le
  SHA annoncé.
- Aucun test n’a été exécuté. La collecte ciblée a seulement collecté deux
  tests. Aucune base PostgreSQL, compilation Typst, publication, provider,
  Bridge ou MinIO n’a été lancé.

## Constats vérifiés

1. **L’oracle manquant est bien inter-seam.** M2B classe #7 comme
   `MISSING_ORACLE`: le test PG valide le V6 canonique, tandis que les tests PDF
   compilent des données synthétiques ou un V6 construit en mémoire; aucun ne
   relie la chronologie complète de l’artefact V6 persisté au PDF final.
   `docs/mission03-architecture/_work/M2B_TEST_SURFACE_ADVISOR_AUDIT_PUB.md:28-34,37-47`

2. **La chronologie attendue est énumérable depuis le document canonique.** Le
   V5 enveloppe un V4 qui porte `timeline` comme tuple ordonné, et le JSON le
   sérialise dans cet ordre. Le V6 expose son `base_publication`; l’oracle peut
   donc prendre `parsed_v6.base_publication.document.timeline` comme source
   d’attente après relecture du blob PG.
   `backend/src/cti_app/domain/publication_document.py:738-746,877-894,2022-2023`

3. **Le hash V5 canonique existe.** `publication_document_v5_sha256` calcule
   SHA-256 du JSON V5 canonique trié et compacté. Le constructeur V6 compare ce
   hash à `provenance.base_publication_sha256`; la publication recalcule aussi
   le hash des octets de base avant augmentation.
   `backend/src/cti_app/domain/publication_document.py:2025-2033,2225-2234`
   `backend/src/cti_app/application/analyst_publication_service.py:87-99`

4. **Il n’existe pas de helper PG commun prêt à appeler, mais un test de
   référence complet existe.** Le fixture `migrated_postgres_url` crée une base
   nommée aléatoirement, migre `head`, puis la supprime. Le test
   `test_postgres_opinion_approval_publishes_new_v6_and_preserves_v5` approuve
   une opinion, publie V6 et vérifie que le blob V5 reste identique, que le V6
   embarque le V5 et que l’opinion ID/version correspondent. Le flux est écrit
   dans le corps du test, pas extrait en helper réutilisable.
   `backend/tests/integration/conftest.py:78-91`
   `backend/tests/integration/test_analyst_opinion_publication_postgres.py:755-809`

5. **Le motif Typst réel + Poppler existe séparément.**
   `test_real_typst_and_poppler_render_v6_opinion_without_changing_v5` compile
   du V5 puis un V6 construit en mémoire avec Typst réel, extrait le PDF avec
   `pdftotext`, et vérifie les éléments de l’opinion. Le test de chronologie
   réelle vérifie le texte exact et le cas à date mensuelle. Ces tests
   n’utilisent pas le V6 persisté en PostgreSQL et n’énumèrent pas toutes ses
   entrées canoniques.
   `backend/tests/test_typst_chp_parity.py:1068-1167,1169-1244`

6. **Les acteurs existent dans la provenance canonique, pas dans le
   `pdf_lineage` actuel.** L’augmentation V6 copie les IDs reviewer/approver
   dans `PublicationDocumentV6ProvenanceV1` et dans les métadonnées de
   l’artefact. Le `pdf_lineage` construit par `_prepare` ne contient dans
   `opinion` que ID, version, statut, SHA et investigation ID; `_prepare` ne
   compare pas les copies reviewer/approver. `with_publication_lineage` ajoute
   ce dictionnaire au render-data canonique. Un test qui exige ces deux acteurs
   dans ce lineage échouerait jusqu’à un changement de production.
   `backend/src/cti_app/application/publication_augmentation.py:192-210,402-411`
   `backend/src/cti_app/application/publication_rendering.py:768-819,126-148`

7. **La sémantique `opinion=None` est déjà explicitement gardée.** Un V6 avec
   opinion absente est valide seulement si les sections/figures et tous les
   champs d’opinion de provenance sont également absents. L’opinion présente
   doit être approuvée et correspondre à la provenance et à la section rendue.
   L’ajout d’un oracle de chronologie n’exige pas de modifier ces invariants.
   `backend/src/cti_app/domain/publication_document.py:2047-2068,2088-2105`

8. **Préconditions hôte constatées.** Le binaire demandé fonctionne et annonce
   `typst 0.15.1 (9dfd3a08)`. `pdftotext`, `pdftoppm`, `pdfinfo` et `pdftocairo`
   sont installés. En revanche, `infra/typst-fonts.lock` demande 102 fichiers
   et aucun des 102 n’existe sous `chpTypst/`, racine utilisée par défaut par
   le fixture; un chemin `FONT_BUNDLE_ROOT` valide pourrait remplacer cette
   racine. La collecte `--collect-only` des deux tests cibles a réussi.
   `backend/tests/test_typst_compiler_runtime.py:177-217`
   `infra/typst-fonts.lock:1-5`

## Périmètre minimal si L5 est redéfini

Pour l’oracle de complétude seul, étendre le test PG existant ou extraire son
montage dans un helper de test partagé; ne pas modifier le renderer ni le
template. Utiliser une base V5 de fixture avec plusieurs entrées de chronologie
à textes uniques, dates exactes et partielles, puis publier l’opinion approuvée
dans la base PostgreSQL jetable. Relire le blob V6 persisté, le projeter par
`TypstRenderer`, compiler avec le Typst épinglé et extraire son PDF avec
`pdftotext`.

Assertions initiales :

- chaque texte et date canonique de `base_publication.document.timeline` apparaît
  une fois dans le texte PDF extrait; les positions des entrées restent dans
  l’ordre canonique;
- les octets du blob V5 d’origine sont inchangés, et
  `publication_document_v5_sha256(V6.base_publication)` égale le hash enregistré
  dans la provenance V6;
- le V6 persistant contient l’ID/version de l’opinion approuvée exacte, son
  statut approuvé, ainsi que les IDs reviewer/approver canoniques;
- le chemin de publication de base conserve `opinion=None` et le lineage
  `nature="base_publication"`.

Ces assertions n’exigent pas une modification éditoriale. Pour inclure les
acteurs dans `pdf_lineage`, il faut d’abord décider si leur exposition doit
rester machine-readable dans le render-data ou apparaître dans la sortie PDF;
le second choix touche la présentation et le template.

## Commandes proposées pour une phase autorisée ultérieure

Commande de référence pour l’oracle Typst/Poppler existant, après fourniture du
bundle de polices verrouillé :

```sh
cd backend
TYPST_BINARY=/snap/typst/current/bin/typst \
FONT_BUNDLE_ROOT=/path/to/locked-font-bundle \
.venv/bin/pytest -q tests/test_typst_chp_parity.py::test_real_typst_and_poppler_render_v6_opinion_without_changing_v5
```

Pour le nouveau test combiné (commande à adapter au nom de test final), le
Makefile crée/utilise PostgreSQL d’intégration et le fixture détruit sa base
jetable après le test :

```sh
TYPST_BINARY=/snap/typst/current/bin/typst \
FONT_BUNDLE_ROOT=/path/to/locked-font-bundle \
make test-integration \
  INTEGRATION_TEST_PATH=tests/integration/test_publication_completeness_postgres.py \
  INTEGRATION_PYTEST_ARGS="-q --tb=short"
```

Ces commandes sont proposées seulement; elles n’ont pas été lancées pendant ce
scout. L’intégration actuelle impose aussi D2, Typst et Poppler à cause de son
test de prévisualisation de protocole; un oracle L5 réduit aux chronologies
devrait éviter les fixtures D2.

## Risques et points restant à décider

- Bundle manquant : aucune compilation réelle n’est possible avec le chemin
  local par défaut tant que les 102 polices attendues ne sont pas installées.
- Temps/flakiness : compilation Typst réelle + extraction Poppler augmente le
  coût; les délais et l’assertion sur texte Unicode doivent rester bornés et
  utiliser des sentinelles uniques pour éviter les doublons accidentels.
- Version : le fixture exige exactement Typst 0.15.1; le binaire actuel
  correspond.
- Le hash de V6 ou de PDF seul ne prouve pas la complétude sémantique; l’oracle
  doit comparer le texte extrait aux entrées canoniques lues depuis le blob V6.
- Le test resterait un rendu offline avec DB jetable; il ne prouverait aucune
  publication, diffusion ou réplication réelle.
- Décision attendue : « lineage reviewer/approver » désigne-t-il les champs
  canoniques V6/métadonnées déjà persistés, ou bien leur présence dans
  `pdf_lineage` généré? Le second choix requiert un changement de production
  différé à une décision éditoriale.
