# Tests manuels locaux

## Préflight et démarrage

Depuis la racine du dépôt :

```bash
git rev-parse HEAD
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose exec worker pandoc --version
```

Conserver le SHA et la version Pandoc observés dans le compte-rendu. Pandoc
doit être disponible dans `worker` avant d’accepter une publication.

Les workspaces explorables directement depuis l’hôte sont :

```text
./var/workspaces/subjects
./var/workspaces/editions
```

Avant toute production réelle, vérifier le bridge :

```bash
make bridge-status
```

Dans Chrome, l’extension `chatgpt-bridge/extension` doit être chargée, le
popup doit afficher une session prête avec `ws://127.0.0.1:8001/ws`, et la
production d’un article de test doit pouvoir atteindre l’étape Références.
Ne lancer aucune production payante tant que `make bridge-status` ne confirme
pas `ready` et que la session de l’extension n’est pas prête. Pour les détails
du bridge, utiliser `make bridge-logs`.

## Test manuel final — édition 2 articles

Ce scénario vise exactement deux articles sélectionnés, dans l’ordre
éditorial A puis B.

1. Dans l’interface sur `http://localhost:5173`, ouvrir une édition en phase
   Sélection. L’édition peut contenir N articles éditorialement éligibles
   (par exemple les 22 de la base locale actuelle) — ce n’est pas un
   problème : le sélecteur du lot de production démarre toujours à
   `0 sélectionné pour ce lot`, aucun article n’étant présélectionné à
   l’ouverture ou au rechargement de la page.

   Dans le sélecteur du lot de production, cocher exactement deux sujets A et
   B (case à cocher, pas la sélection éditoriale), puis noter :

   ```text
   Edition ID = <edition-id>
   Subject A ID = <subject-a-id>
   Subject B ID = <subject-b-id>
   ```

   Vérifier que le sélecteur affiche `2 sélectionnés pour ce lot` et que le
   bouton affiche `Lancer la production de 2 articles` — jamais le nombre
   total d’articles éligibles.

2. Cliquer sur `Lancer la production de 2 articles`. Le premier retour doit
   afficher simultanément :

   ```text
   Article A = En cours
   Article B = En attente
   0 / 2 articles traités
   ```

   Le deuxième retour doit afficher :

   ```text
   Article A = Prêt
   Article B = En cours
   1 / 2 articles traités
   ```

   Le retour terminal doit afficher les deux articles prêts et :

   ```text
   2 / 2 articles traités
   ```

   Puis ouvrir `Revue de publication`. Un seul article ne doit jamais être
   déclaré terminé avant l’autre.

3. Dans la Review, vérifier qu’il y a exactement deux cartes : position 1 = A
   et position 2 = B. Ouvrir A, revenir à la Review, puis ouvrir B. Les titres
   et le contenu affichés doivent correspondre au bon sujet.

   Dans l’onglet `Pipeline` de chacun des deux sujets, vérifier aussi les
   quatre étapes `Références`, `Extraction`, `Synthèse` et `Assemblage` à
   l’état réussi. Les fichiers `items/001-…/article/publication.json`,
   `items/001-…/article/publication.md` et leurs équivalents `002-…` doivent
   être présents, non vides et correspondre au bon sujet.

   Ouvrir `Diagnostics` sur chaque carte et noter pour chacune :

   ```text
   run_id
   generation
   artifact_id
   ```

   Les deux `run_id` et les deux `artifact_id` doivent être distincts.

4. Vérifier que A et B sont inclus, puis cliquer sur `Accepter la production`.
   Attendre `Bulletin publié`, puis vérifier la présence du lien :

   ```text
   Télécharger le bulletin DOCX
   ```

5. Définir le workspace de cette édition avec le vrai chemin observé :

   ```bash
   export EDITION_ID='<edition-id>'
   export EDITION_WORKSPACE="var/workspaces/editions/2026-08_IR"
   find "$EDITION_WORKSPACE" -maxdepth 4 -type f | sort
   python -m json.tool "$EDITION_WORKSPACE/release/publication-manifest.json"
   python -m json.tool "$EDITION_WORKSPACE/release/edition.json"
   unzip -p "$EDITION_WORKSPACE/release/bulletin.docx" word/document.xml \
     | rg -n 'Article A|Article B'
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
         article/publication.md
         sources/manifest.json       # si des sources sont présentes
         assets/manifest.json        # si des assets sont présents
       002-<subject-b-slug>/
         pipeline/production-state.json
         article/publication.json
         article/publication.md
         sources/manifest.json       # si des sources sont présentes
         assets/manifest.json        # si des assets sont présents
     release/
       publication-manifest.json
       edition.json
       edition.md
       bulletin.docx
   ```

   Les quatre fichiers de `release/` doivent être présents. Vérifier que le
   manifest contient exactement deux entrées, positions 1 et 2, et que le XML
   DOCX contient A avant B.

6. Télécharger le DOCX depuis l’interface, puis comparer le téléchargement au
   fichier du workspace :

   ```bash
   export DOWNLOADED_DOCX='<chemin-vers-le-docx-téléchargé>'
   sha256sum "$DOWNLOADED_DOCX" \
     "$EDITION_WORKSPACE/release/bulletin.docx"
   ```

   Les deux hashes doivent être identiques.

## Deuxième passage — cache obligatoire

Le produit conserve l’édition publiée en lecture seule : une édition déjà
publiée ne peut pas recevoir un nouveau batch. Le passage cache doit donc être
préparé avant le batch cible, dans une édition encore en Sélection (ou dans une
nouvelle édition de test si le premier passage est déjà publié).

Depuis l’onglet `Pipeline` de chaque sujet, effectuer d’abord une production
individuelle A1 puis B1 et attendre `prête` pour chacune. Cette étape crée les
artefacts coûteux de référence.

Revenir à l’édition de test, vérifier qu’A et B sont les deux sujets
sélectionnés, puis lancer le batch cible `2 articles`. Ce nouveau batch produit
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

La réponse doit toujours exposer la release publiée, et l’interface doit
toujours afficher Review/release et le lien DOCX. La base PostgreSQL, MinIO et
les workspaces montés doivent avoir conservé leurs données.

**NE JAMAIS utiliser `docker compose down -v` sur des données manuelles que
l’on souhaite conserver.**

## Workspace jetable et rematérialisation canonique

La release locale est une projection jetable. Sauvegarder d’abord le hash du
DOCX canonique/local, puis supprimer uniquement le répertoire ciblé :

```bash
export RELEASE_WORKSPACE="$EDITION_WORKSPACE/release"
sha256sum "$RELEASE_WORKSPACE/bulletin.docx"
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
test -f "$RELEASE_WORKSPACE/edition.md"
test -f "$RELEASE_WORKSPACE/bulletin.docx"
sha256sum "$RELEASE_WORKSPACE/bulletin.docx"
```

La release, le manifest et le DOCX doivent être restés lisibles pendant que la
projection locale était absente, et le hash du DOCX restauré doit être
identique à celui sauvegardé avant suppression. Cette opération ne crée ni
Release ni Manifest et ne fait aucun appel modèle ; elle lit uniquement
PostgreSQL et les blobs canoniques.

## Arrêt

Sans effacer les données :

```bash
docker compose stop
```

ou :

```bash
docker compose down
```
