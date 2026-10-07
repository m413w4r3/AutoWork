# Extraction : cadrage par cas, empreinte stable, sources dupliquées

Statut : spécification validée par le propriétaire produit le 2026-10-07. Trois correctifs liés,
à implémenter dans cet ordre (C2 → C3 → C1). Observabilité hors périmètre (traitée ailleurs).

## Constat chiffré (run réel, Subject GTG-30004)

- Les deux sources CORE du sujet sont `…/threat-intelligence-report-september-2026` et la même URL
  avec `?page=1` : un rapport de 1,18 Mo qui traite une douzaine de cas sans rapport entre eux
  (`GTG-15001`, `GTG-20006`, `GTG-30004`, …). Le sujet ne concerne qu'un seul cas.
- Le profil `full` découpe chaque source en tranches (`archived_source_chunks`, 14 appels pour ce
  rapport) : environ 28 appels modèle, plus d'une heure, pour deux sources quasi identiques, dont
  la plus grande partie du texte est hors sujet.
- Les deux URL sont identiques à 99,98 % : seul le payload JavaScript Next.js intégré
  (`self.__next_f.push`) diffère.
- Entre deux collectes de la même URL, les octets HTML changent (noms de fichiers de build
  `/_next/static/chunks/*.js|css`, attributs `role=`) alors que le texte d'article change peu :
  `source_extractions` a été recalculé 9 fois en une journée sur ces deux URL. La clé d'identité
  d'`source_extractions` utilise `source_content_sha256`, c'est-à-dire le hash du HTML brut.
- Conséquence qualité : toutes les extractions de ce rapport remontent les facts et IOC de tous les
  cas ; la classification a dû être corrigée a posteriori (`INDICATOR_SECTION_OTHER_CASE`).

## C2 — Empreinte de cache fondée sur le texte d'évidence effectif

Objectif : deux collectes qui donnent le même texte d'évidence utilisé par l'extraction ne
déclenchent qu'une extraction.

- Définir `effective_evidence_sha256` : SHA-256 du texte exact fourni à l'extracteur (le
  `parsed_text` du `SourceEvidenceDocument` après normalisation déterministe, **après** cadrage C1
  lorsqu'il s'applique). Le HTML brut reste conservé tel quel dans MinIO et son hash sert encore à
  la provenance et à `ProductionSourceDocument`.
- L'identité du cache d'extraction (`source_extractions`, contrainte `uq_source_extractions_identity`)
  et l'identité des checkpoints d'extraction utilisent `effective_evidence_sha256` à la place du hash
  HTML brut. Toutes les autres composantes de l'identité (contrat, prompt, parseur, vérificateur,
  contrat de texte, politiques modèle/routage/profil) restent inchangées.
- Si un changement de schéma est nécessaire (nouvelle colonne, nouvel index unique), fournir une
  migration Alembic minimale et réversible (dernière migration : `0002_production_batch_pause`) ;
  sinon réutiliser la colonne existante en documentant le changement de sémantique. Les anciennes
  lignes restent lisibles mais ne sont plus réutilisées (versionner).
- Vérifier empiriquement sur les quatre états réels du rapport (hashes HTML `74edd474`, `e82a4067`,
  `17e79a15`, `1aa8d48b`, blobs `source-decoded/…` dans MinIO) quels couples produisent le même
  texte d'évidence ; documenter le résultat. Si le texte effectif diffère encore à cause d'éléments
  volatils (identifiants de build, payload `self.__next_f`, attributs `role`), étendre la
  normalisation du texte d'évidence (pas de la collecte) pour les exclure, sans retirer aucun
  contenu d'article.
- Invariants : aucune preuve ne peut s'appuyer sur un texte absent de la source archivée ; le gate de
  preuves source (`production_source_evidence.py`) continue de travailler sur le même texte que celui
  qui a été haché.

## C3 — Sources dupliquées : une seule extraction

Objectif : une variante d'URL du même article (ex. `?page=1`) ne double ni les appels ni les
facts/IOC.

- Après C2, deux sources d'un même sujet dont le `effective_evidence_sha256` est identique sont
  extraites **une fois**. La seconde reçoit `reuse_state = "duplicate_content"` et référence l'extraction de la
  première (même `canonical_blob_id`), sans appel modèle et sans doubler ses facts, événements,
  indicateurs et règles dans l'artifact `EXTRACTION` (une seule occurrence par valeur ; la
  provenance garde les deux `source_document_id` quand le modèle de données le permet, sinon
  la source canonique seule).
- Pour deux textes non identiques mais quasi identiques (même hôte et même chemin, requête
  d'URL qui ne change que `page`/`utm_*`, ratio de similarité du texte d'évidence ≥ 0,999), appliquer
  le même traitement, derrière une constante nommée et documentée ; en cas de doute, ne pas
  dédupliquer (conservateur).
- Les deux URL restent des références du sujet (section RÉFÉRENCES) : seule l'extraction est
  mutualisée. Vérifier l'effet sur `extraction_progress`, la projection de pertinence, la
  publication (`source_document_ids` d'un indicateur), la QA et la review.

## C1 — Extraction cadrée sur le cas du sujet pour les rapports multi-cas

Objectif : n'envoyer au modèle que les sections du rapport relatives au cas du sujet.

- Détection : un document est multi-cas lorsque ses titres de structure exposent au moins deux
  intitulés de cas explicites (réutiliser la détection déjà introduite pour la classification
  d'IOC : `decode_indicator_section_paths`, `_CASE_SECTION_HEADING`, identifiants `GTG-\d{5}` ou
  « Case study »). Ne pas ajouter une seconde logique parallèle : extraire un utilitaire partagé.
- Cadrage : pour un sujet dont le titre ou l'acteur/campagne porte un identifiant de cas (ex.
  `GTG-30004`), ne conserver que (a) les sections dont le chemin de titres nomme ce cas, (b) le
  préambule/résumé du document avant le premier titre de cas. À défaut d'identifiant de cas, ou si
  le cadrage produit un texte vide ou ambigu (plusieurs sections de cas nommées par le sujet sans
  correspondance), **ne pas cadrer** et extraire le document entier (comportement actuel). Le
  cadrage est déterministe et sans appel modèle.
- Traçabilité : l'artifact d'extraction et `extraction_progress` indiquent
  `scope: {kind: "case", case_id, kept_sections, total_sections, kept_chars, total_chars}` et un
  avertissement stable (`extraction_scoped_to_case:GTG-30004:3/14`). Le texte cadré est celui qui est
  haché pour C2, donc deux sujets avec des cas différents n'ont jamais la même entrée de cache, et
  deux sujets du même cas la partagent.
- Les règles de classification introduites pour `INDICATOR_SECTION_OTHER_CASE` restent en filet de
  sécurité (ceinture et bretelles) et ne doivent pas régresser.
- Cas particulier des sources `technical` de type CSV d'IOC globaux (ex. `*_IOCs.csv` avec colonne
  `gtg` par ligne) : si le CSV porte une colonne de cas, ne garder que les lignes du cas du sujet
  (même principe, déterministe) ; sinon comportement actuel.

## Versions, tests, validation

- Incrémenter les versions qui entrent dans l'identité d'extraction (contrat de texte source,
  version du gate de preuves, politique de profil) pour que les anciens résultats ne soient pas
  réutilisés comme preuve du nouveau comportement.
- Tests de non-régression : empreinte stable sur deux versions réelles du rapport (fixtures
  réduites tirées des HTML réels, pas les 1,2 Mo) ; dédoublonnage `?page=1` ; cadrage multi-cas
  (cas du sujet conservé, autres cas écartés, document mono-cas inchangé, fallback sans identifiant) ;
  CSV à colonne de cas ; identité de cache différente entre deux cas ; aucune écriture de facts
  d'un autre cas.
- Validation sur le scénario réel (par le superviseur, après déploiement) : le Subject GTG-30004
  extrait en 2 à 4 appels au lieu de ~28 ; les Subjects GTG-30005, -30006, -34007 et GTG-30004
  partagent le même document source mais pas les mêmes extractions cadrées.
