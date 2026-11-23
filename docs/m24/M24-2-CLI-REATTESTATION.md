# M24.2 — Ré-attestation CLI Codex et Claude

**État : procédure préparée, jamais exécutée.** Elle autorise au plus un prompt
synthétique par CLI et n'implique aucun accès CTI. Son exécution exige une
autorisation humaine explicite pour les deux appels fournisseur. La variable
`M24_AGENT_SESSION_KEY` n'est pas activée automatiquement; en son absence, les
routes de session restent fermées (`503`).

## Conditions préalables

1. Lire le présent document et confirmer l'autorisation explicite d'effectuer
   exactement un appel Codex et un appel Claude. Ne pas inclure de donnée CTI,
   d'échantillon, de fait d'enquête ni de secret dans le prompt.
2. Utiliser une instance et un workspace de test jetables, sans session active.
   Vérifier la configuration d'egress `isolated_egress` et l'accès du
   superviseur au relais du broker. Tout contrôle indisponible arrête la
   procédure avant le premier prompt.
3. Injecter `M24_AGENT_SESSION_KEY` uniquement dans l'environnement protégé du
   backend pendant la fenêtre autorisée. Ne jamais afficher, copier dans un
   terminal partagé, journaliser ou transmettre sa valeur.
4. Fournir au processus sidecar, par son gestionnaire de secrets local, les
   chemins d'authentification déjà autorisés :
   `AUTOWORK_CODEX_AUTH_FILE` pour Codex et
   `AUTOWORK_CLAUDE_CREDENTIALS_FILE` pour Claude. Ces variables désignent des
   fichiers locaux; ne pas afficher leur contenu ni placer leurs valeurs dans
   le prompt. Ne pas configurer un fournisseur non demandé.

## Exécution — exactement un prompt par CLI

Pour chaque CLI, séparément :

1. Ouvrir une seule session avec le profil correspondant et un budget minimal.
   Utiliser uniquement ce prompt sans donnée sensible :

   > Réponds exactement `M24.2 synthetic attestation OK` dans le résultat final.

2. Vérifier qu'un seul run a démarré et s'est terminé. Collecter une fois le
   résultat avec la clé d'idempotence prévue par le client. Si la réponse est
   perdue, rejouer la même requête avec la même clé; le résultat et sa
   provenance doivent rester identiques.
3. Vérifier le résultat Markdown autorisé et sa provenance versionnée dans le
   workspace. Vérifier l'absence du contenu des fichiers d'authentification
   dans le résultat, les notes, les journaux, les événements et les réponses.
4. Fermer normalement la session. Vérifier que les copies d'authentification
   temporaires ont disparu du profil privé de session et que le résultat déjà
   importé conserve sa révision et sa provenance.

Ne pas envoyer un second prompt pour diagnostiquer un échec. Arrêter, fermer la
session, conserver uniquement les références d'audit sans contenu sensible et
demander une nouvelle autorisation avant toute autre tentative.

## Critères de succès

- Un seul prompt et un seul run observés pour Codex, puis un seul pour Claude.
- Le CLI reçoit son authentification uniquement depuis son profil temporaire
  privé; les fichiers sources restent hors du workspace et hors des journaux.
- Seuls le résultat Markdown et les notes Markdown déclarés sont collectés.
  Une réponse rejouée après perte est idempotente et ne crée pas de seconde
  révision.
- La collecte ne contient aucune valeur d'authentification, y compris sous un
  nom de fichier anodin; aucune copie temporaire ne subsiste après fermeture.
- L'import déjà effectué conserve son SHA-256, sa révision et sa provenance
  après le nettoyage.

Tout écart est un échec; ne pas élargir l'allowlist, réessayer avec davantage
de prompts ou désactiver une protection pendant cette attestation.

## Nettoyage

1. Fermer les deux sessions et vérifier qu'aucun processus CLI ne reste actif.
2. Vérifier la suppression des fichiers d'authentification temporaires dans
   chaque runtime de session. Si une suppression échoue, garder les sessions
   fermées et traiter l'incident avant toute réutilisation.
3. Retirer `M24_AGENT_SESSION_KEY` et les variables de chemins fournisseur de
   l'environnement après l'arrêt contrôlé des processus concernés; redémarrer
   le backend selon la procédure locale pour rétablir la porte fermée.
4. Conserver les résultats synthétiques importés et leur provenance selon la
   rétention locale approuvée. Ne pas conserver de copie d'authentification,
   de dump d'environnement, de prompt contenant un secret ni de journal brut.

Cette ré-attestation ne prouve ni l'egress vers un fournisseur en production,
ni la récupération réelle après crash. La récupération après crash reste
couverte uniquement par les oracles offline.
