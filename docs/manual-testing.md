# Tests manuels locaux

## Préflight et démarrage

Depuis la racine du dépôt :

```bash
git rev-parse HEAD
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose exec worker typst --version
```

Conserver le SHA et la version Typst observés dans le compte-rendu. Le worker
doit pouvoir produire le PDF final.

Les workspaces explorables directement depuis l’hôte sont :

```text
./var/workspaces/subjects
./var/workspaces/editions
```

Pour tester une route qui utilise ChatGPT Bridge, `OPENAI_BRIDGE_BASE_URL`
doit pointer vers un serveur Bridge externe disponible. La gestion du serveur
Bridge et de son extension Chrome relève de son repository autonome.

## Test manuel final — édition 2 articles

Ce scénario vise exactement deux sujets sélectionnés, dans l’ordre
éditorial A puis B.

1. Dans l’interface sur `http://localhost:5173`, ouvrir le Dashboard
   `/editions/{edition_id}`, puis naviguer vers la capacité explicite
   `/editions/{edition_id}/selection`. L’édition peut contenir N sujets
   éditorialement éligibles
   (par exemple les 22 de la base locale actuelle) — ce n’est pas un
   problème : le sélecteur du lot de production démarre toujours à
   `0 sélectionné pour ce lot`, aucun sujet n’étant présélectionné à
   l’ouverture ou au rechargement de la page.

   Dans le sélecteur du lot de production, cocher exactement deux sujets A et
   B (case à cocher, pas la sélection éditoriale), puis noter :

   ```text
   Edition ID = <edition-id>
   Subject A ID = <subject-a-id>
   Subject B ID = <subject-b-id>
   ```

   Vérifier que le sélecteur affiche `2 sélectionnés pour ce lot` et
   que le bouton affiche `Lancer la production de 2 sujets` — jamais le nombre
   total de sujets éligibles.

2. Cliquer sur `Lancer la production de 2 sujets`. Le premier retour doit
   afficher simultanément :

   ```text
   Sujet A = En cours
   Sujet B = En attente
   0 / 2 sujets traités
   ```

   Le deuxième retour doit afficher :

   ```text
   Sujet A = Prêt
   Sujet B = En cours
   1 / 2 sujets traités
   ```

   Le retour terminal doit afficher les deux sujets prêts et :

   ```text
   2 / 2 sujets traités
   ```

   Puis naviguer explicitement vers `/editions/{edition_id}/review` (`Revue de
   publication`). Un seul sujet ne doit jamais être déclaré terminé avant
   l’autre.

3. Depuis `/editions/{edition_id}/review`, vérifier qu’il y a exactement deux
   cartes : position 1 = A et position 2 = B. Ouvrir A, revenir à la Review,
   puis ouvrir B. Les titres et le contenu affichés doivent correspondre au
   bon sujet.

   Dans l’onglet `Pipeline` de chacun des deux sujets, vérifier aussi les
   quatre étapes `Références`, `Extraction`, `Synthèse` et `Assemblage` à
   l’état réussi. Les fichiers `items/001-…/article/publication.json` et son
   équivalent `002-…` doivent être présents, non vides et correspondre au bon
   sujet.

   Ouvrir `Diagnostics` sur chaque carte et noter pour chacune :

   ```text
   run_id
   generation
   artifact_id
   ```

   Les deux `run_id` et les deux `artifact_id` doivent être distincts.

4. Vérifier que A et B sont inclus, puis cliquer sur `Accepter la production`.
   Naviguer vers `/editions/{edition_id}/publication`, attendre `Bulletin
   publié`, puis vérifier que le preview affiche `EditionDocumentV2` et que le
   téléchargement PDF est disponible. `EditionRelease` contient uniquement
   les JSON gelés ; son rendu est une ligne `EditionRender` indépendante.

   ```text
   GET /api/subjects/{subject_id}/publication/pdf
   GET /api/editions/{edition_id}/preview/pdf?preview_input_hash=<sha256>
   POST /api/editions/{edition_id}/release/render
   GET /api/editions/{edition_id}/release/pdf
   ```

   Les jobs `publication.edition.assemble` et `publication.edition.render`
   séparent le gel JSON de la compilation Typst. Un retry de rendu ne relance
   ni Assembly ni Production.

5. Définir le workspace de cette édition avec le vrai chemin observé :

   ```bash
   export EDITION_ID='<edition-id>'
   export EDITION_WORKSPACE="var/workspaces/editions/2026-08_IR"
   find "$EDITION_WORKSPACE" -maxdepth 4 -type f | sort
   python -m json.tool "$EDITION_WORKSPACE/release/publication-manifest.json"
   python -m json.tool "$EDITION_WORKSPACE/release/edition.json"
   test -s "$EDITION_WORKSPACE/release/bulletin.pdf"
   sha256sum "$EDITION_WORKSPACE/release/bulletin.pdf"
   ```

   Le suffixe `2026-08_IR` est un exemple : utiliser `YYYY-MM_<country_code>`
   correspondant à l’édition. La structure attendue est :

   ```text
   var/workspaces/editions/<YYYY-MM_COUNTRY_CODE>/
     manifest.json
     items/
       001-<subject-a-slug>/
         pipeline/production-state.json
         article/publication.json
         sources/manifest.json       # si des sources sont présentes
         assets/manifest.json        # si des assets sont présents
       002-<subject-b-slug>/
         pipeline/production-state.json
         article/publication.json
         sources/manifest.json       # si des sources sont présentes
         assets/manifest.json        # si des assets sont présents
     release/
       publication-manifest.json
       edition.json
       bulletin.pdf
   ```

   Les trois fichiers de `release/` doivent être présents. Vérifier que le
   manifest contient exactement deux entrées, positions 1 et 2, et ouvrir le
   PDF pour confirmer que les articles A puis B suivent cet ordre. Le fichier
   PDF correspond à un `EditionRender` identifié par `input_hash`.

6. Télécharger le PDF depuis l’interface, puis comparer le téléchargement au
   fichier du workspace :

   ```bash
   export DOWNLOADED_PDF='<chemin-vers-le-pdf-téléchargé>'
   sha256sum "$DOWNLOADED_PDF" \
     "$EDITION_WORKSPACE/release/bulletin.pdf"
   ```

   Les deux hashes doivent être identiques.

## Deuxième passage — cache obligatoire

Le produit conserve l’édition publiée en lecture seule : une édition déjà
publiée ne peut pas recevoir un nouveau batch. Le passage cache doit donc être
préparé avant le batch cible, dans une édition encore ouverte (ou dans une
nouvelle édition de test si le premier passage est déjà publié).

Depuis l’onglet `Pipeline` de chaque sujet, effectuer d’abord une production
individuelle A1 puis B1 et attendre `prête` pour chacune. Cette étape crée les
artefacts coûteux de référence.

Revenir au Dashboard de l’édition de test, naviguer vers
`/editions/{edition_id}/selection`, vérifier qu’A et B sont les deux sujets
sélectionnés, puis lancer le batch cible `2 sujets`. Ce nouveau batch produit
A2 puis B2 séquentiellement et doit afficher, pour chaque article, dans l’onglet
`Pipeline` :

```text
Références : réutilisée depuis un calcul précédent
Extraction CTI : réutilisée depuis un calcul précédent
Synthèse : réutilisée depuis un calcul précédent
```

Ouvrir `Diagnostics` pour relever `reused_from_artifact_id`, `calcul original`
et `research_date`. Les `run_id` A2/B2 doivent être nouveaux ; les artifacts de
PUBLICATION doivent eux aussi être nouveaux. La Review et le manifest final
doivent utiliser A2 et B2, jamais A1/B1.

Il n’existe pas de compteur CLI global de dépenses modèle. La preuve opérateur
supportée est le détail Pipeline/Diagnostics ci-dessus, complétée si besoin
par les logs du worker :

```bash
docker compose logs --tail=500 worker backend
```

Lorsqu’un `model_run_id` est connu, son diagnostic sûr peut être consulté
ainsi :

```bash
make model-run-diagnostics RUN_ID='<model-run-id>'
```

Ne pas utiliser de workflow automatisé avec ChatGPT ou le bridge réel pour
valider ce smoke.

## Retry manuel depuis EXTRACTION — coûteux

Ce test déclenche volontairement de nouveaux appels modèle. Ne l’exécuter
qu’après avoir sauvegardé les preuves du passage cache, ou sur un article de
test dédié.

1. Ouvrir le sujet, onglet `Pipeline`.
2. Dans `Relancer depuis une étape…`, choisir `Extraction`.
3. Attendre la fin du run et vérifier :

   ```text
   Références conservée
   Extraction recalculée
   Synthèse recalculée
   Publication recalculée
   ```

   La nouvelle génération ne doit pas signaler Extraction ou Synthèse comme
   réutilisées. Vérifier le nouveau `generation` dans `Diagnostics`.

## Persistance Docker

Après une publication valide :

```bash
docker compose down
docker compose up -d
docker compose ps
curl -fsS "http://localhost:8000/api/editions/$EDITION_ID/release"
```

La réponse doit toujours exposer la release JSON publiée, et l’interface doit
toujours afficher Review/release et le lien PDF. La base PostgreSQL, MinIO et
les workspaces montés doivent avoir conservé leurs données.

**NE JAMAIS utiliser `docker compose down -v` sur des données manuelles que
l’on souhaite conserver.**

## Workspace jetable et rematérialisation canonique

La release locale est une projection jetable. Sauvegarder d’abord le hash du
PDF canonique/local, puis supprimer uniquement le répertoire ciblé :

```bash
export RELEASE_WORKSPACE="$EDITION_WORKSPACE/release"
sha256sum "$RELEASE_WORKSPACE/bulletin.pdf"
rm -rf -- "$RELEASE_WORKSPACE"
test ! -e "$RELEASE_WORKSPACE"
```

Ne supprimer aucune donnée PostgreSQL ou MinIO. Reconstruire la projection par
l’endpoint technique borné :

```bash
curl -fsS -X POST \
  "http://localhost:8000/api/editions/$EDITION_ID/release/materialize"
test -f "$RELEASE_WORKSPACE/publication-manifest.json"
test -f "$RELEASE_WORKSPACE/edition.json"
test -f "$RELEASE_WORKSPACE/bulletin.pdf"
sha256sum "$RELEASE_WORKSPACE/bulletin.pdf"
```

La release JSON, le manifest et le PDF doivent être restés lisibles pendant que
la projection locale était absente, et le hash du PDF restauré doit être
identique à celui sauvegardé avant suppression. Cette opération ne crée ni
EditionRelease ni PublicationManifest et ne fait aucun appel modèle ; elle lit
uniquement PostgreSQL et les blobs canoniques. Pour tester un rendu indépendant,
appeler `POST /api/editions/{edition_id}/release/render`, puis télécharger le
résultat par `GET /api/editions/{edition_id}/release/pdf`.

## Arrêt

Sans effacer les données :

```bash
docker compose stop
```

ou :

```bash
docker compose down
```
