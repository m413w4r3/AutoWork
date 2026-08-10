# Collecte sûre des sources et extraction de preuves

## Déclenchement et frontières

La collecte ne démarre jamais lors de la sélection éditoriale. L'analyste ouvre le Workbench d'un
`Subject` sélectionné puis appelle explicitement `POST /api/subjects/{subject_id}/collection`.
Cette action crée le job canonique `source.collect`; Dramatiq ne transporte que son identifiant.
Le handler reprend les sources `failed_retryable`, `archived` ou `extracted` et ignore celles déjà
`completed`. Une annulation reste coopérative via le contexte de job entre deux sources.

Le collecteur est un adaptateur typé. Les tests remplacent DNS et transport HTTP par des doubles et
n'ouvrent aucune connexion. Le collecteur de production :

- accepte uniquement HTTP et HTTPS, sans credentials dans l'URL ;
- bloque localhost, metadata cloud et toute adresse IPv4/IPv6 privée, loopback, link-local,
  multicast, réservée ou non globale ;
- contrôle deux réponses DNS puis connecte l'adresse approuvée, avec validation TLS du nom d'hôte ;
- recommence ces contrôles après chaque redirection ;
- borne la durée totale, les redirections, les octets réseau, les octets après décompression et le
  ratio de décompression ;
- détecte HTML ou PDF depuis les octets au lieu de faire confiance à `Content-Type` ;
- n'exécute ni JavaScript, macro, script, binaire ni échantillon.

Les domaines peuvent en plus être bornés par `COLLECTION_ALLOWED_DOMAINS` et
`COLLECTION_BLOCKED_DOMAINS` (listes séparées par des virgules).

## Modèle canonique et stockage

| Table | Contenu | Mutabilité |
| --- | --- | --- |
| `source_collections` | Source candidate rattachée au Subject, état et relation proposée | projection mutable |
| `collection_attempts` | URL, redirections, horodatages, statut, MIME, taille, hash, job et erreur | append-only |
| `source_documents` | Observation sémantique d'une URL acquise | métadonnées canoniques |
| `derived_artifacts` | Texte dérivé, parseur/version et métadonnées de publication | append-only |
| `claims` | Valeur extraite, méthode, source et offsets | append-only |
| `indicators` | Valeurs originale/normalisée, type, source et offsets | append-only |
| `human_decisions` | Validation, correction ou rejet par un acteur | append-only |

Les octets bruts vont dans le bucket logique `source-raw`, le texte UTF-8 dans `source-text`. Ils
sont adressés par SHA-256 et jamais stockés dans PostgreSQL. Deux URL retournant le même contenu
partagent donc un blob mais gardent deux `SourceDocument` et deux historiques de tentatives.

Les états sont :

```text
queued -> fetching -> archived -> extracted -> completed
                   \-> unavailable
                   \-> blocked
                   \-> failed_retryable
                   \-> failed_terminal
```

Une reprise ne modifie ni ne supprime une tentative passée. La migration additive `0007` installe
des triggers PostgreSQL refusant `UPDATE` et `DELETE` sur tentatives et artefacts de preuve.

## Extraction et validation

Le parseur HTML ignore les zones `script`, `style`, `noscript`, `template` et `svg`. Le parseur PDF
extrait uniquement le texte et les métadonnées ; il n'exécute aucun contenu embarqué. Chaque texte
est un artefact dérivé versionné.

L'extracteur déterministe reconnaît hash, domaine, IP, URL, CVE, identifiant ATT&CK et email,
y compris les formes defangées. La valeur originale et la normalisation sont toutes deux gardées.
Qwen reçoit uniquement le texte, explicitement marqué comme donnée distante non fiable, et propose
une sortie structurée pour acteurs, campagnes, malwares, outils, chaîne d'infection, TTP,
victimologie, faits, évaluations et incertitudes. Un nom, une date, un IOC ou une CVE est rejeté si
sa valeur littérale n'est pas présente dans le passage exact fourni.

Une correction crée une `HumanDecision` avec valeur originale et valeur corrigée. Le `Claim` ou
l'`Indicator` initial n'est jamais écrasé. De même, une relation de source proposée par un modèle
reste `provisional`; `verified` exige une preuve déterministe `deterministic:*` ou une décision
humaine `human:*`.
