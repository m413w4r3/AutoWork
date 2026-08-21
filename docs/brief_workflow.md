# Parcours evidence-first d’une brève

## Pack gelé

`POST /api/subjects/{subject_id}/brief/freeze` est une action explicite, réservée à un groupe
sélectionné comme `brief`. Le service construit une projection à partir des seules sources
archivées et des dernières décisions humaines. Un claim ou IOC sans décision `validate` ou
`correct` est exclu ; un objet rejeté est exclu. Les corrections sont incluses sans modifier
l’extraction originale.

Le contenu canonique comprend exclusivement les catégories suivantes : sources archivées,
claims validés, IOC validés, entités normalisées, incertitudes et décisions humaines relatives aux
preuves. Chaque entrée possède un `object_hash`, et `object_hashes` énumère tous ces hashes. Le JSON
canonique est stocké dans le bucket logique `brief-evidence-packs`; son SHA-256 devient le
`content_hash`. PostgreSQL conserve la version, le hash, la projection et la référence au blob.

Un gel identique réutilise la version existante. Dès qu’une preuve ou décision de preuve change,
le hash change et une version additive est créée. Les tables `brief_evidence_packs` et
`brief_drafts` sont protégées par des triggers append-only définis dans le schéma PostgreSQL.
Un brouillon est automatiquement `stale` lorsque son `pack_id` n’est plus celui du pack courant ;
aucune mise à jour rétroactive n’est nécessaire.

## Génération et édition

Qwen local est la route par défaut (`standard_draft`). OpenAI via `chatgpt-bridge` utilise la route
`premium_synthesis` et n’est autorisé que si toutes les sources du pack permettent l’envoi externe
et si aucune ne porte `do_not_submit`. Le modèle reçoit uniquement le JSON du pack gelé, le guide de
style versionné et l’instruction de rédaction. Il ne reçoit ni workspace, ni document brut, ni
conversation.

`POST /brief/generate` et la régénération d’un bloc soumettent un job canonique
`brief.generate`; Dramatiq ne transporte que son identifiant. La clé d’idempotence couvre le hash
du pack, la version de brouillon parente, le fournisseur, le bloc et l’instruction. L’exécuteur
synchrone remplace Redis dans les tests.

La sortie structurée impose un titre, un à trois blocs, des phrases typées, leurs `claim_ids` et
`indicator_ids`, les limites et les références. Une régénération de bloc ne transmet pas le
brouillon précédent : elle recrée un bloc depuis le pack puis produit une nouvelle version en
conservant les autres blocs. Une édition humaine d’un bloc produit également une version additive
et conserve les rattachements phrase → preuves.

## QA, décision et export

Avant enregistrement et approbation, le validateur vérifie :

- que chaque phrase factuelle référence au moins un claim ;
- que claims, IOC et sources référencés appartiennent au pack ;
- que l’analyse déterministe du texte ne révèle aucun IOC absent de la liste validée ;
- que le brouillon utilise le pack courant.

`request-changes`, `approve` et `promote` ajoutent une `HumanDecision` attribuée à l’acteur. La
promotion n’est possible qu’après approbation et transforme explicitement le format éditorial du
groupe de `brief` vers `major`. L’export Markdown n’est disponible que pour le brouillon courant
approuvé ; il contient le texte, les limites et les URLs avec le SHA-256 des sources archivées.

Les décisions de cycle de rédaction ne rentrent pas dans le hash du pack de preuves : seules les
décisions qui qualifient les preuves sont figées. Cela évite qu’une approbation invalide elle-même
le texte qu’elle vient d’approuver.
