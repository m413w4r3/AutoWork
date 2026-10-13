# Interface d’enquête et avis de l’analyste

L’onglet **Enquête / Avis de l’analyste** du sujet affiche la synthèse exacte
visée par le lancement (run, artefact, version, état vérifié et SHA-256
canonique), les enquêtes déjà liées à ce sujet et le détail de l’enquête
sélectionnée. Les constats montrent leur évaluation, leur nouveauté, leurs
réserves et les références d’observations. Les échantillons affichent leur
origine et leur qualification. Les observations exposent uniquement leurs
métadonnées datées et leur SHA-256 ; aucun blob, contenu brut ou secret n’est
chargé par cet écran.

L’écran présente également les affirmations vérifiées, les analyses et pistes,
les versions de règles et leurs essais par rôle, le budget par dimension, les
capacités indisponibles comme limites locales et les décisions bloquées. Le
journal d’outils n’est pas le contenu principal. L’avis conserve ses blocs
facultatifs renseignés dans l’ordre éditorial, avec qualification, confiance,
citations, historique et violations de citation renvoyées par le serveur.
L’approbation cible explicitement une version et son hash. La publication crée
une PUBLICATION augmentée ; l’écran rappelle que le réépinglage dans la
composition est une décision humaine distincte et fournit le lien vers cette
composition.

## Routes consommées

- `GET /api/subjects/{subject_id}/production/artifacts/synthesis` pour afficher
  la version exacte lançable.
- `GET|POST /api/subjects/{subject_id}/investigations` pour lister et lancer.
- `GET /api/investigations/{investigation_id}` pour lire et suivre l’enquête.
- `POST /api/investigations/{investigation_id}/resume` et `/interrupt` pour
  reprendre ou interrompre sous CAS et clé d’idempotence.
- `GET /api/investigations/{investigation_id}/opinion` et
  `POST .../opinion/compose` pour lire ou composer l’avis.
- `POST /api/opinions/{opinion_id}/revise`, `/approve`, `/request-changes` et
  `/publish` pour réviser, décider et proposer une PUBLICATION augmentée.
- `GET /api/investigations/{investigation_id}/detections`, puis
  `POST /api/detections/{version_id}/approve|reject` pour la revue humaine des
  règles.
- `GET /api/investigations/{investigation_id}/envelope` et
  `POST .../envelope/approve|extend|revoke` pour les décisions G2 versionnées.
- `GET /api/investigations/{investigation_id}/samples` et
  `POST .../samples/{association_id}/qualify` pour l'historique et les décisions
  G3.
- `POST /api/investigations/{investigation_id}/requests` pour demander une
  analyse ciblée au prochain tour et reprendre automatiquement une enquête en
  pause.

Les actions envoient les identifiants exacts, les versions/hash CAS attendus et
un `Idempotency-Key` lorsque l’API le prend en charge. L’acteur est résolu et
persisté côté serveur depuis l’identité authentifiée. Le lancement est lui aussi
associé à la synthèse vérifiée exacte ; l’API garantit son rejeu par cette
identité.

## Décisions G2/G3 et demandes ciblées

L’enveloppe locale initiale reste non approuvée en version 0 et n’accorde aucun
fournisseur ni droit d’acquisition. L’analyste peut l’approuver avec des
limites multidimensionnelles, un volume maximal explicite, des fournisseurs
disponibles et des bornes de divulgation. L’extension est une action distincte
avec `expected_version`; la révocation ajoute une version auditée et retire les
droits externes. Chaque formulaire exige une raison et une confirmation
explicite. Il rappelle que les pivots passifs autorisés n’exigent plus de clic,
que l’acquisition est bornée par le volume et que la qualification reste
humaine.

Les décisions G3 chargent l’association exacte et son historique. Une
association `seed` est signalée comme déjà présente dans l’article étudié; elle
n’est pas présentée comme une nouveauté. Une association acquise reste candidate
jusqu’à une décision humaine. Confirmer ou rejeter exige une raison et au moins
une référence d’observation de la même enquête. Les changements lié, incertain
ou candidat restent motivés. L’acteur vient de l’identité authentifiée côté
serveur et n’est jamais envoyé par le navigateur.

Toutes les mutations portent `Idempotency-Key`; les décisions G2/G3 incluent
leur `correlation_id`, et les extensions/qualifications portent la version CAS
courante. Une erreur de conflit conserve son code API typé dans l’interface.
Les mutations invalident le détail d’enquête, les listes du sujet, l’enveloppe
et les échantillons.

La demande ciblée accepte un texte de 1 à 2 000 caractères et une cible
facultative `hash`, `ioc` ou `claim_id`. Elle indique que la demande sera traitée
au prochain tour et que l’API relance automatiquement l’enquête si elle était
en pause.
