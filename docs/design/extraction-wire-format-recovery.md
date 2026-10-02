# Plan — extraction tolérante, timeline et incertitudes (audit du run #3, 2026-10-02)

## Constat (preuves : run a95663b2, sujet d9d24833)

1. **Extraction JSON stricte = régression.** Depuis AW-011 (S03/S05), l'extraction
   canonique demande un objet JSON (`Q2SourceOutput.model_validate_json`).
   Le bridge ChatGPT produit du JSON invalide :
   - marqueur `:chatgpt-content-reference{index="0"}` inséré *dans* une chaîne JSON ;
   - guillemets internes non échappés (`The value "362091310" appears…`).
   Un seul défaut = `SOURCE_OUTPUT_INVALID` pour toute la source, le checkpoint
   n'est pas écrit, et chaque relance repaie l'appel (MuddyWater : 558 s + 242 s
   perdues, 3 tentatives identiques). Un parseur Markdown tolérant existait avant
   (`parse_q2_proposals_markdown`, format `FACT`/`EVENT`/`IOC`/`RULE`) et sert
   encore au batch et au repair.
2. **Chunking naïf** : coupe au milieu d'un mot (« Address-po ») ; le modèle
   signale ensuite une « capture incomplète ».
3. **Timeline** : construite *déterministiquement* depuis `events` de l'extraction
   (`build_synthesis_timeline`), pas depuis le prompt REFERENCES. Donc :
   - texte en anglais (le prompt d'extraction ne demande pas le français) ;
   - tri incohérent : les `date_text` (« mid-2023 », « Q2 2026 », « Also that
     year ») sont tous après les dates ISO, non triés entre eux ;
   - événements hors sujet (Necurs 2013, Glupteba 2019, DPRK) non filtrés.
4. **Incertitudes** : ce n'est pas la synthèse qui les écrit mais l'union des
   `uncertainties` d'extraction (`build_synthesis_uncertainties`). Le prompt
   d'extraction dit seulement « the unresolved points of the capture » → bruit
   en anglais (« No complete YARA rule… », limites du schéma, capture tronquée).
5. Qualité aval (phase 3, décision produit) : sources « supporting » extraites
   en `ioc_rules` seulement (Check Point = 25 hashes sans contexte, aucun fait) ;
   IOC « confirmés » sans contexte ni lien au sujet ; noms de fichiers génériques
   (`main.py`) et contacts de pied de page ; enrichissement pauvre (tableau
   2 lignes redondant, graphe linéaire 4 nœuds, 0 figure).

## Principes

- Le bridge n'est jamais sommé de produire du JSON. Format compact orienté lignes,
  tolérant : un item mal formé est **écarté avec un warning**, jamais la source.
- Le texte brut du bridge est nettoyé (marqueurs de citation) avant parsing ;
  le brut est archivé tel quel.
- Une sortie déjà obtenue n'est jamais re-demandée si elle peut être re-parsée.
- Langue de publication = français pour tout texte *rédigé* (events, contexte,
  incertitudes) ; `evidence_quote`, valeurs techniques et règles restent littérales.
- Les checkpoints sont adressés par contenu : toute évolution de prompt ou de
  parseur incrémente la version correspondante.

## Lots

### Phase 1 — Codex A : extraction tolérante (propriétaire de `production_prompts.py`)
- A1. Nettoyage du texte bridge (`:chatgpt-content-reference{…}`, `cite…`,
  `entity[…]`, autres marqueurs d'UI) avant parsing, avec test sur les deux
  échantillons réels ci-dessus.
- A2. Remplacer la demande JSON par le wire format compact pour FULL, IOC_RULES
  et batch : `FACT <catégorie>`, `EVENT <date|texte>`, `IOC <confirmed|contextual>
  <type>`, `RULE`, `UNCERTAINTIES`. Parseur item-level tolérant (catégorie/type
  inconnu → item ignoré + warning ; en-tête cassé → on resynchronise à la
  ligne suivante ; `empty`/`unavailable` conservés).
  `ProductionSourceExtractionService.invoke` consomme le texte via ce parseur,
  plus de `model_validate_json` ni de schéma injecté dans le prompt.
  Incrémenter `CANONICAL_*_PROMPT_VERSION` (nouveaux checkpoints, anciens intacts).
- A3. Une source n'échoue que si **zéro** item exploitable ET sortie non vide
  non reconnue ; sinon succès partiel avec warnings.
- A4. Chunking sur frontières de paragraphes/lignes avec léger recouvrement
  et dédoublonnage des items fusionnés.
- A5. Prompts : events (`text`, `context`) et faits en **français**, date
  absolue obligatoire (ISO si publiée, sinon `date_text` normalisé et
  absolu : « fin 2024 », « T2 2026 » — jamais « la même année » / « also that
  year »), chronologie limitée à ce que la publication décrit comme faisant
  partie de son sujet principal.
- A6. Prompts : `UNCERTAINTIES` en français, **analytiques seulement**
  (attribution douteuse, chiffres/dates contradictoires, niveau de confiance,
  lien non établi). Interdits : absence de règle YARA/Sigma, limites du
  schéma/types d'artefacts, capture tronquée/chunk, classification de fichiers,
  banalités. Maximum 5 par source ; liste vide préférée au bruit.
- A7. REFERENCES prompt : incertitudes en français, événements triés
  chronologiquement, dates absolues ; incrémenter `REFERENCES_PROMPT_VERSION`.

### Phase 1 — Codex B : timeline et incertitudes déterministes (ne touche PAS `production_prompts.py`)
- B1. `timeline_sort_key` : résoudre `date_text` en date approximative triable
  (FR/EN : « mid-2023 », « late 2024 », « Q2 2026 », « février 2025 », « 2013 »,
  « début 2025 »), tri total stable ; `date_text` reste affiché ; non résolu →
  fin de liste.
- B2. `build_synthesis_timeline` : dédoublonnage plus robuste (même date +
  texte normalisé), rejet des entrées à date relative non résoluble
  (« also that year ») avec warning.
- B3. `build_synthesis_uncertainties` : dédoublonnage insensible à la casse /
  ponctuation, filtre de sécurité sur les motifs de bruit connus (règles
  absentes, schéma, capture/chunk tronqué) au cas où un ancien checkpoint en
  contient, plafond d'affichage.
- B4. Tests unitaires pour chaque point avec les entrées réelles du run #3.

### Phase 2 — Codex C : intégration et revue
- Lint, typecheck, tests ciblés puis `make test-backend`; relecture croisée
  des diffs A et B ; vérification que les anciens checkpoints restent lisibles ;
  test de bout en bout avec un faux bridge renvoyant des sorties « sales »
  (marqueur de citation, ligne inconnue, guillemets) → succès partiel.

### Phase 3 — décisions produit (non lancé)
- Passer en profil FULL les sources « supporting » indépendantes pertinentes
  (Check Point) pour que la synthèse croise plusieurs sources.
- Exiger contexte + lien au sujet pour classer un IOC `confirmed_ioc` ;
  filtrer noms de fichiers génériques et contacts de pied de page.
- Filtre de pertinence de la timeline par sujet (sélection côté synthèse,
  validée par evidence refs).
- Enrichissement : tableaux multi-sources, graphes croisés, figures issues du PDF.

## Contraintes d'exécution
- Python 3.12 via `uv`/`make` uniquement (voir `.claude/CLAUDE.md`).
- Le dépôt contient des modifications non commitées étrangères
  (discovery, `model_gateway.py`, `config.py`, `integrations/*`…) :
  ne jamais les annuler ; toucher `model_gateway.py` seulement si indispensable,
  par éditions minimales.
- Aucun test supprimé/affaibli/skip. Pas de commit.
