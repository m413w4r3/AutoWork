# ARCHIVE — Nimbalyst operator session configuration (NON-NORMATIVE)

> This is a retained historical Nimbalyst-specific reference. `spawn_session(model=..., effortLevel=...)` and its default model/effort are **not** Paseo configuration. For current coding orchestration see [Paseo agent orchestration](paseo-agent-orchestration.md) and the host's effective profiles. Do not copy these settings into AutoWork API payloads.

---

# Modèles opérateur et sous-sessions Nimbalyst

## Propriété des réglages

Le modèle et l’effort de raisonnement de la session opérateur, ainsi que le lancement de ses enfants, appartiennent à l’orchestrateur Nimbalyst. AutoWork conserve l’investigation et ses preuves; ses routes `MODEL_ROUTE_*` ou `OPENAI_*_MODEL` ne règlent pas la session Codex opérateur. Ne pas ajouter ces secrets ou ces réglages de session à un payload AutoWork.

Valeurs par défaut actuelles pour l’opérateur et les sous-sessions :

| Réglage | Valeur |
|---|---|
| Modèle | `openai-codex:gpt-6-luna` |
| `effortLevel` | `xhigh` |

Le lanceur transmet les choix au moment de la création via `spawn_session(model=..., effortLevel=...)`. Nimbalyst enregistre les valeurs effectives dans les métadonnées de chaque session. Un changement ultérieur de configuration s’applique aux nouveaux lancements, pas rétroactivement aux sessions existantes.

## Choisir le modèle et l’effort

- Garder Luna avec `xhigh` pour le parent opérateur : il rassemble les preuves, compare les hypothèses, choisit la prochaine action et reste responsable de la décision.
- Garder ce défaut pour une sous-session qui fait une analyse statique longue, relie plusieurs observations ou rédige/revoit un lot éditorial complexe.
- Pour une tâche strictement bornée (extraction d’un fait depuis une section, contrôle mécanique de références, résumé court), choisir un effort ou un modèle moins coûteux seulement si Nimbalyst l’offre pour `spawn_session`. Le parent vérifie le résultat à partir des sources avant de l’utiliser.
- Utiliser le même niveau pour des tâches qui demandent des jugements comparables. Noter tout override effectif pour que les rapports et coûts restent interprétables.

Le niveau d’effort n’élargit jamais les autorisations. Un modèle plus capable ne reçoit pas plus de fichiers, d’outils, de secrets ou de droits de mutation.

## Quand déléguer

Créer une sous-session lorsque le travail est indépendant et assez long pour avoir son propre mandat et rapport :

- analyse statique de plusieurs méthodes ou artefacts, en lecture seule;
- rédaction Bridge d’un lot d’unités cohérentes, une unité identifiable par résultat;
- vérification phrase par phrase des références, versions ou preuves;
- revue indépendante d’une contradiction, d’une limite de preuve ou d’un dossier de faits.

Garder dans le parent le choix de l’hypothèse globale, les mutations, l’attachement de propositions, le rapprochement final et toutes les décisions G3/publication. Ne pas déléguer une tâche qui n’a pas de critère de fin clair ou dont le résultat dépend d’un enfant précédent sans expliciter cette dépendance. Respecter la capacité Nimbalyst; placer les autres tâches en attente plutôt que de perdre leur périmètre.

## Contrat de délégation

Le parent ouvre chaque sous-session avec un mandat court et vérifiable :

```text
OBJECTIF : une question et une décision que le résultat doit éclairer.
PÉRIMÈTRE : fichiers, sections, versions et artefacts autorisés.
OUTILS : lecture seule par défaut; outils nommés explicitement si nécessaires.
INTERDIT : aucune exécution d’échantillon, aucun contact C2, aucune mutation
            ou publication sans mandat distinct; aucun secret transmis.
RÉSULTAT : constat, sources/version/SHA, faits et inférences séparés,
           alternative ou limite, critère atteint/non atteint, coût observé.
FIN : chemin du rapport ou réponse attendue et condition de retour au parent.
```

Fournir des pointeurs et extraits strictement utiles plutôt qu’un historique entier. Ne jamais transmettre un jeton `OPERATOR_*_TOKEN`, une clé VT/Shodan, un DSN, un mot de passe, une URL signée ou un autre secret opérationnel, y compris dans les variables d’environnement, arguments, pièces jointes ou texte du prompt. Un enfant ne reçoit pas le jeton GitHub de l’attaquant pour l’utiliser; il ne peut le traiter que comme un indicateur CTI passif si sa valeur publiée fait partie des éléments autorisés. Aucun jeton d’accès n’est nécessaire pour une tâche de lecture seule.

Lecture seule est le défaut. Si un enfant ne peut pas effectuer une tâche sans permission de mutation ou credential, il s’arrête et rend le besoin au parent; le parent décide et exécute séparément l’action autorisée. Chaque proposition reste non fiable jusqu’à sa vérification par le parent. Les enfants ne valident ni avis ni publication.

## Traçabilité et coûts

Dans les métadonnées Nimbalyst et le rapport d’opération, relier :

- l’ID de session parent et l’ID enfant;
- le mandat, les fichiers/versions autorisés, le modèle et `effortLevel` effectifs;
- les heures de début/fin, le statut et le pointeur vers le rapport enfant;
- les coûts ou unités que Nimbalyst expose pour cette session, séparés du coût Bridge/API AutoWork.

Si une mesure de coût n’est pas exposée, écrire `indisponible`; ne pas la déduire du nombre d’appels ni du volume de réponse. Le parent vérifie les sources et décide si le résultat entre dans l’analyse canonique. L’ID et le rapport de session décrivent l’orchestration; ils ne deviennent pas des preuves de l’investigation.

## Configuration proposée

Bloc sans secret, à conserver comme configuration opérateur dans `.autowork-runs/operator-config.toml` ou à saisir dans le lanceur Nimbalyst :

```toml
[operator]
model = "openai-codex:gpt-6-luna"
effortLevel = "xhigh"

[operator.subsessions]
model = "openai-codex:gpt-6-luna"
effortLevel = "xhigh"
```

Ce fichier est un support de configuration proposé; ne pas supposer que Nimbalyst le charge automatiquement. Le lanceur lit ces champs puis les passe à `spawn_session`. L’override de lancement, s’il existe, doit apparaître dans les métadonnées de la session créée. Aucun jeton, mot de passe, chemin d’hôte ni contenu de `.env` ne figure dans ce fichier.
