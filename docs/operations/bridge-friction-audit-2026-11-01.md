# Audit Bridge AutoWork — réduction des frictions (1er novembre 2026)

**Base auditée :** `origin/main@571748ead0dbe75a2333b9139f8e527830971f8d`. Cette note est distincte de `docs/mission11/M11_REPORT.md`, qui conserve ses états intermédiaires historiques. Les correctifs M11 déjà poussés sont dans le code, mais leur déploiement courant ne peut pas être confirmé par GitHub seul.

## Objectif métier

Bridge doit être utilisé utilement pour **stratégie de pivot, analyse critique de YARA, rétro du protocole C2, candidats Suricata et rédaction/revue de l'avis**. Il conseille et produit des brouillons ; les mesures locales et les observations canoniques établissent les faits. Aucune obligation de consultations par nombre, aucune limitation artificielle des prompts et aucune nouvelle approbation.

## Audit des étapes

| Étape | Constat sur main | Simplification ou suite |
| --- | --- | --- |
| Admission externe | Les refus `osint_policy_denied` avant tout envoi venaient de rapports VT `file` à hash non associé, pas du transport Bridge. `_split_unassociated_hash_observations` les omet du **dossier automatique** et les trace. Les références explicites restent soumises à la politique. | **Conserver** le contrôle réel du coordinateur : ne pas le contourner ; guider l'opérateur vers une référence autorisée plutôt qu'un retry. |
| Dossier C2/Suricata | `_documented_protocol_model_facts` expose `ProtocolModelV1` avec `observed:false`, `documented_*`. | Ne pas exiger de PCAP pour une **consultation de conception** Suricata ; validation/replay de règle restent dépendants d'une capture. |
| Prose Suricata | `local_suricata_prose_v1` préserve des règles référencées par les faits sans exiger `advisor-wire.v1`. | Conserver le texte source, sa provenance et son statut `candidate/untested`. |
| Réponse YARA | La critique YARA de M11 était utile, mais les motifs CIL jokerisés proposés n'étaient pas liés aux mesures exactes du catalogue ; `rule.propose_bridge` ne peut pas les canoniser. | Ne **pas** affaiblir la vérification de motif : utiliser la critique pour `rule.revise` avec motifs mesurés, ou obtenir une nouvelle mesure locale, puis `rule.test`. Une critique utilisable n'est pas une règle validée. |
| Erreur `measurements_invalid` | Le code de `_select_verified_measurements` fournit des `measurement_errors`, formes admises et différences de clés, dans la réponse de refus pré-transport. | L'opérateur doit reprendre ces détails et l'une des formes exactes (`content_pattern_id` OU `blob_id,kind,offset,value`) ; ne pas inventer des identifiants. Pas de répétition identique. |
| Taille et contenu | `_sample_inventory` attachait par défaut le contenu base64 complet des samples associés à des observations, y compris pour une consultation rédactionnelle ou Suricata. Un dossier de M11 a atteint ~12 Mo. | Dans cette PR, **sélection sémantique** : pas d'octets de sample entier pour `draft_text`, `review_text`, `suricata_draft`, `figure_plan`. Les descripteurs, références et faits techniques restent. Aucun plafond en octets, aucune troncature des matériaux pertinents. |
| Avis `draft_text` | Une réponse structurée en « 1. Échantillon A » et « 2. Échantillon B » peut être conservée comme `unstructured_overall` et rester inutilisable comme blocs `bridge_authored`. L'attribution par rang serait risquée. | Nouveau repli **seulement pour une unité explicitement ciblée** : texte intégral exact attribué à cette unité, avec même SHA et provenance Bridge. Plusieurs unités ambiguës restent non attribuées ; demander à Bridge des repères U1/U2 ou cibler une unité. |
| Longues opérations | Un `running` de 24 min a finalement abouti ; les fichiers joints très volumineux et l'attente dans le relais sont distincts du temps fournisseur. | Vérifier l'invocation et son statut réel, ne pas renvoyer un POST ambigu. La taille du prompt n'est pas un motif de refus supplémentaire. |
| Extension externe | Selon le journal M11, au-delà de ~200 Ko le projet `chatgpt-bridge` dépose le prompt en pièce jointe et une consigne générique reste dans le composer ; le format de réponse peut être moins suivi. | Hypothèse à vérifier **dans le dépôt Bridge**, non modifié ici. Le parseur AutoWork doit rester tolérant à la prose. |

## Comportement conseillé aux opérateurs

- **Après premiers invariants** : consultation `pivot_strategy` ou `plan_investigation` sur une question discriminante si elle aide à trouver des samples.
- **YARA** : envoyer la règle candidate et les motifs **mesurés** pour critique ; conserver les recommandations utiles même si une règle nouvelle est non canonisable ; tester localement les modifications appliquées.
- **C2** : demander l'interprétation d'un code de parsing, d'une transition ou d'un schéma cryptographique avec statut statique/observé.
- **Suricata** : avec un modèle statique seulement, demander un brouillon explicitement `untested` et évaluer le risque de bruit ; avec PCAP, valider et rejouer offline si disponible.
- **Avis** : privilégier un petit ensemble de références canoniques et une section explicitement ciblée lorsque le modèle ignore les repères d'unités ; l'intégration `bridge_authored` exige le **texte exact** et son `text_sha256`. Ne pas forcer un mapping de plusieurs sections par leur ordre.
- **Dans tous les cas** : consulter Bridge quand sa critique ou sa rédaction peut faire progresser l'enquête ; il n'y a pas de nombre minimal ni maximal de consultations. Un avis utile en prose ne doit pas être jeté au seul motif qu'il ne donne pas une source YARA canonisable.

## Vérification de la PR

1. Rejouer une réponse `draft_text` en prose avec **une seule unité** explicite : un bloc exploitable du type exact demandé est disponible et correspond au texte source.
2. Avec **plusieurs unités** sans U1/U2 ou libellé identifiable : aucune attribution heuristique par rang n'est ajoutée.
3. Pour un dossier Suricata/éditorial avec un sample associé : conserver hash, association, métadonnées et observations, sans mettre les octets PE complets en pièce jointe par défaut. Le chemin qui nécessite les octets (notamment YARA) continue de fonctionner.
4. Les instructions de rendu demandent des repères U1/U2 en **prose**, pas du JSON ni un nouveau format obligatoire.
5. Vérifier `python -m pytest backend/tests/test_advisor_wire.py backend/tests/test_investigation_advisor_tools.py` depuis l'environnement Python du dépôt. Tests réellement exécutés à renseigner après CI ; les ajouts n'ont pas été exécutés pendant l'édition par connecteur.

## Avant un `make clean`

- Ne pas assimiler « déployé » à « commité ». Sur le **main distant audité**, les correctifs M11 cités plus haut existent, mais le contenu du working tree hôte n'est pas visible par ce connecteur.
- Sur l'hôte : consulter `git status --porcelain=v1`, `git diff --stat`, `git diff --cached --stat`, `git log -5 --oneline`, et l'inventaire des fichiers non suivis. Commettre ou archiver toute modification souhaitée **avant** nettoyage.
- Ne pas nettoyer `.autowork-runs/m11`, les observations, le relais et les artefacts non exportés tant que la réconciliation des invocations et leur sauvegarde ne sont pas effectuées.
- Fusionner ou reprendre explicitement la PR d'audit et vérifier un checkout propre de la révision souhaitée. GitHub seul ne prouve ni l'état des conteneurs, ni la sauvegarde des runs, ni l'absence de correctifs locaux supplémentaires.

**Non-objectifs :** pas de modification des garde-fous G2/TLP, de la politique de provenance, de l'extension `chatgpt-bridge`, ni d'autorisation d'effets externes supplémentaires.
