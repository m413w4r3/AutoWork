# Skills opérationnels CTI — lots 1 et 2

Guides métiers génériques sans changement des politiques d'investigation. Vérifier le chargement pour chaque runtime. Les contrats actifs et les outils annoncés par l'application prennent le pas sur les exemples de noms d'outils.

## Objectif

Favoriser **invariants techniques, règles YARA/Suricata candidates, description du protocole C2 et nouveaux échantillons**. Supprimer les obligations méthodologiques ajoutées sans nécessité : nombre minimal d'hypothèses/tests/pivots ; expérience ou finding à chaque lecture ; goodware et holdout préalables à la première candidate. L'inventaire available_tools, les schémas et les droits courants restent l'autorité.

Les dix guides vivent dans .agents/skills et sont indexés dans .agents/skills/OPERATOR-CTI.md :
- autowork-cti-investigation-loop
- autowork-bridge-collaboration
- autowork-tool-strategy
- autowork-artifact-triage
- autowork-cti-pivot-strategy
- autowork-evidence-to-finding
- autowork-dotnet-analysis
- autowork-pe-native-analysis
- autowork-yara-bridge-workflow
- autowork-network-protocol-detection

Les compétences métier sont génériques, sans constats historiques ni identifiants d'enquête embarqués. Les cas AppDomainManager et YARA M08 cités sont des exemples conceptuels, pas des résultats à injecter dans de nouvelles enquêtes.

## Distinction des acteurs

- **Paseo/coding-agent :** peut découvrir .agents/skills et, selon provider, .claude/skills, dans le dépôt. Son travail en worktree ne produit pas de preuve CTI.
- **Opérateur CTI :** voit uniquement l'API de son enquête et son répertoire de session approuvé, conformément à OPERATOR_BRIEF.md. Ne peut pas supposer lire directement les guides du dépôt. Un chargement explicite est nécessaire.
- **Sous-agents :** reçoivent uniquement les guides pertinents et des références de l'enquête actuelle ; ne pas répliquer une archive complète, un ancien dossier ou du contexte d'une autre enquête.

## Préparer une session isolée

Sur l'hôte d'orchestration autorisé, **avant de démarrer** l'opérateur :

~~~bash
python3 scripts/ops/stage_operator_cti_skills.py --workspace /chemin/vers/session-autorisée
~~~

Le script refuse d'écraser un bundle déjà présent. Il copie exclusivement les dix SKILL.md et crée un manifeste local contenant leurs SHA-256. Il ne lit pas les dossiers secrets, ne contacte aucun fournisseur et ne lit aucune trace d'investigation.

Pour une nouvelle session Claude Code, si son répertoire de travail est bien ce workspace, il est possible d'ajouter l'option --claude afin de créer des symlinks .claude/skills vers ce bundle. Pour Codex, vérifier la découverte .agents/skills sur la version installée ; un runtime CTI utilisant un autre mécanisme doit recevoir un pointeur explicite dans son prompt.

Dans le brief de la session, ajouter seulement : « Les guides métiers génériques approuvés sont dans .agents/skills ; utiliser .agents/skills/OPERATOR-CTI.md pour les déclencheurs et lire uniquement les SKILL.md nécessaires. Le registre available_tools et l'enveloppe actuelle restent prioritaires. » L'index OPERATOR-CTI.md est également copié par le script.

**Règle d'isolation :** les skills sont des instructions génériques ; aucun argument d'API, échantillon, observation, finding, SHA ou conclusion d'une autre enquête ne doit être mis dans le bundle. Un nouveau runtime peut ne pas auto-découvrir des skills ; la vérification effective est obligatoire.

## Acceptation : tests de comportement, non auto-déclaration

Tester avec fixtures ou replay hors ligne, sans appel de fournisseur live ni malware exécuté :

1. **.NET opaque :** l'agent utilise field.data.read si offert ; xxd reste une alternative si un offset fichier est connu. Il valorise les octets pour invariants, YARA et pivots.
2. **ETW :** un type AppDomainManager observé ne devient pas une désactivation ETW ou l'exécution de RuntimeBroker.exe.config ; un fait étroit peut être supported.
3. **Pivots :** deux imports génériques partagés ne deviennent pas « même famille » ; l'agent choisit une comparaison discriminante.
4. **Résultat VT empty :** conserve la couverture, ne conclut pas à une absence générale.
5. **Expérience :** la formalisation est correcte lorsqu'elle est utile ou requise par le contrat ; une inspection simple ne crée pas obligatoirement d'hypothèse, d'expérience ou de finding.
6. **YARA :** première candidate possible avec un seul spécimen mesurable. Goodware et holdout renforcent la validation ultérieure mais ne sont pas prérequis à la rédaction/compilation.
7. **Récupération :** ne double pas un POST ambigu ; corrige un CAS à partir d'une lecture de version.
8. **C2 / Suricata :** sans capture, description statique du protocole et candidate non testée ; avec capture, validation/replay offline lorsque disponibles.
9. **Isolation :** un opérateur neuf découvre les guides sans données d'une autre enquête et conserve G2/TLP et l'API comme autorité.

Mesures utiles : invariants nouveaux, candidates YARA/Suricata produites, protocole C2 documenté, nouveaux candidats pertinents, règle améliorée grâce aux variantes et limites connues. Les scénarios de replay ci-dessus servent à mesurer la qualité des skills ; **ils ne constituent pas des formalités obligatoires par investigation**.

## Validation automatique du paquet

~~~bash
python3 scripts/ops/test_stage_operator_cti_skills.py
~~~

Ce test contrôle la présence des dix guides, leur frontmatter et le chargement isolé/rejet d'écrasement. Il **ne prouve pas** que Paseo, Codex ou Claude ont effectivement chargé les guides ; vérifier cette étape dans un nouveau runtime.

Copier [le modèle de mandat opérateur](operator-mandate-template.md) comme `MANDATE.md` dans le workspace; sa checklist `FINAL_REPORT.md` est facultative. Le déroulement actif des échanges Bridge et les appels minimaux aux outils figurent dans [le playbook opérateur](operator-playbook-medium.md) et les skills concernés. Le dépôt ne contient pas de générateur de `MANDATE.md`, `PLAYBOOK.md` ou `ENVIRONMENT.md` pour le workspace.

## Règles de maintenance

Un skill sert de routeur **méthodologique**, jamais de duplication d'un schéma backend ou d'une liste exhaustive de noms d'outils. Après évolution du backend, tester les déclencheurs et les cas réels avant d'éditer les guides. Conserver les règles de preuve et de gouvernance dans leurs contrats canoniques, sans les réécrire contradictoirement ici.
