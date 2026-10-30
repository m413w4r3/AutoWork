# Audit A — refus VT et lisibilité des pivots

**Dépôt audité :** HEAD c921848c301407aa8718b833ab35793d3e0bbeca  
**Périmètre :** traces .autowork-runs/m07c et code source au HEAD ci-dessus. Audit en lecture seule. Seul ce rapport est créé. Aucun appel fournisseur ni test exécuté.

## Conclusion

Les traces contiennent trois refus C et deux refus A antérieurs avec preuve framework de non-envoi. Elles ne contiennent aucun appel VT refusé sous préfixe B. Les quinze appels vt.intelligence_search du journal sont réussis ; je ne trouve donc pas de recherche refusée à attribuer dans cet archive.

Pour les trois refus C, la cause la plus probable est le garde de provenance des références : les pivots proposés sont des opérations sur un hash MD5, alors que le résolveur de références VT n’attache une référence sample_hash qu’aux SHA-256. Le dépôt exige ensuite des références de source avant consommation. **Le code interne exact n’est pas visible dans les réponses enregistrées** : l’API remplace le code borné de refus par tool_failed et un message générique. La cause par invocation reste donc une inférence à forte confiance, pas un fait vérifié dans l’outbox.

Les snapshots autour des trois appels C montrent G2 approuvé, non révoqué, fournisseur VT autorisé, TLP Clear, do_not_submit=false, 72 unités VT consommées et 0 réservée (versions 314 à 318). Ces refus ne sont pas expliqués par un veto G2/TLP/révocation/budget visible. Les observations archivées portent pages=0, provider_routes=[] et une preuve framework de refus avant soumission. Le budget VT de l’investigation reste à 72 avant et après les trois appels.

## Invocations refusées et couverture constatée

| Invocation | Pivot identifié dans les traces | Statut et observation persistée | Interprétation |
|---|---|---|---|
| c070-md5-file-report — 25c12d4a-e193-4d85-99a2-d01278079fd9 | vt.file_report, candidat MD5 | failed; observation de refus 61745f2a-3320-5f13-8235-63fb44864fdb; 0 page et aucune route fournisseur | Cause probable : référence de provenance requise absente |
| c076-contacted-domains-md5 — 6c036754-565f-41b7-982a-8976ff004e8d | vt.relationships, contacted_domains sur candidat MD5 | failed; observation de refus ac6512e7-36f5-547a-9b62-30b94e31749a; 0 page et aucune route fournisseur | Même garde probable |
| c078-contacted-ips-md5 — 848da938-688e-4457-bdaa-2039df7e82a0 | vt.relationships, contacted_ips sur candidat MD5 | failed; observation de refus 1b3c7941-9491-55c1-bae2-4bdc6f400842; 0 page et aucune route fournisseur | Même garde probable |
| a156-vt-pivot-file_report — 37773de1-314d-4c60-abd5-d98d6cb57f67 | vt.file_report, candidat MD5 | failed; refus avant envoi, 0 page/route | Même classe de garde probable ; code interne non persisté |
| a177-vt-pivot-relationships — ab669df2-a8f6-4e65-bbff-b284d288e16d | vt.relationships, pivot IPv4/résolution selon le journal opérateur | failed; refus avant envoi, 0 page/route | Cause précise inconnue ; références de source absentes ou non concordantes possibles |

Les observations ci-dessus sont des **observations de refus AutoWork**, pas des réponses ou observations VirusTotal. Les réponses HTTP 200 sont celles de l’API opérateur qui a enregistré l’échec ; elles ne signifient pas que le fournisseur a été appelé.

a118-vt-behaviour-pcap-s5 (needs_review) est un cas différent : l’archive indique external_effect_unconfirmed, pas une preuve de refus avant envoi. Il ne faut pas le reclasser comme refus certain ni comme résultat VT.

Dans le journal, aucun chemin /operator/tools/vt.* n’a un identifiant b*. Les opérations B présentes dans l’outbox ne constituent donc pas un refus VT attribuable à B dans cet archive. De même, les quinze invocations vt.intelligence_search sont succeeded ; aucune recherche échouée n’est enregistrée ici. Ces constats bornent l’archive M07c, pas d’éventuelles traces externes ou absentes.

## Garde probable des refus C

Le chaînage de code vérifié est le suivant :

1. investigation_service.py:2162-2218, particulièrement 2177-2182, ne construit une référence sample_hash que pour un SHA-256 ; un hash MD5/SHA-1 produit une liste de références vide.
2. investigation_pivots.py:103-120 propose pourtant vt.file_report et les relations de fichier contacted_ips/contacted_domains pour MD5/SHA-1, sans référence de source dans leurs arguments. Les requêtes C observées contiennent les sélecteurs de l’opération, pas de référence de source.
3. virustotal_external_effects.py:105-124 résout les références d’un hash de fichier et ne retourne aucune référence pour une valeur qui n’est pas un SHA-256 ; :156-174 exige des références de source pour les familles VT.
4. external_effects.py:1247-1253 refuse avant consommation quand les références attendues sont absentes (source_references_required). Le coordinateur retourne alors un refus de phase pré-consommation ; virustotal.py:2083-2095 le convertit en exception de refus avant nouvel envoi.
5. investigation_vt_tools.py:534-564 conserve un code interne borné dans ToolResult.error_code, mais operator_tool_invocation.py:1473-1478 et :1526-1535 l’écrasent dans la réponse opérateur par tool_failed et le texte générique.

**Classé friction de provenance, pas garde à supprimer.** La vérification des références, de leur investigation, de leur association à la cible et des restrictions de source protège la provenance. La friction vient du désaccord entre les pivots publiés comme disponibles et les références que le résolveur sait réellement autoriser. Pour C, source_references_required est la meilleure explication du statut failed, car ce code retombe sur la branche FAILED de _pre_send_refusal_status (investigation_vt_tools.py:1889-1906). La réponse enregistrée ne prouve toutefois pas ce code pour chaque invocation.

Pour a177, le point de refus pré-consommation est établi par l’observation ; le code source requis pour une relation non-fichier examine les références fournies et leur association à un IOC confirmé (investigation_service.py:2252-2297). Les données conservées ne permettent pas de dire si l’IPv4 était non confirmée, si une référence manquait, ou si une autre validation a refusé l’opération. Ne pas attribuer ce refus à un motif exact sans le code interne.

## Couverture et autres frictions vérifiées

La couverture des pivots utilise une comparaison exacte des arguments normalisés (investigation_pivots.py:47-61) et ne considère pas FAILED comme couvert. C’est approprié : un refus pré-envoi ne prouve pas qu’une lecture fournisseur a été faite. Ce mécanisme n’explique donc pas les refus C et il ne faut pas affaiblir la déduplication exacte.

La vue de couverture perd cependant de l’information utile à l’opérateur. ToolCoverageEntryV1 expose invocation, outil, un seul parameter_key, statut, observation et résumé (research_context_builder.py:3340-3379). _coverage_parameter_key ne conserve que le premier sélecteur préféré (:3939-3967), ce qui ne distingue pas nécessairement deux relations sur le même objet. Les arguments complets ne sont utilisés que pour la comparaison interne des pivots. L’absence d’une ligne de couverture lisible « pivot → statut → observation » est donc une friction d’observabilité, distincte du garde de provenance.

Deux défauts adjacents méritent un test ciblé, sans preuve qu’ils aient causé les trois refus C :

- **Coût d’un refus prouvé pré-envoi.** Le gestionnaire reconnaît la preuve PREPARATION/PRE_CONSUME et les compteurs d’effet à zéro (investigation_vt_tools.py:534-564) mais ne passe pas zero_cost=True. _result peut alors calculer VT_UNITS avec state.api_requests (:750-775), compteur que l’adaptateur incrémente avant l’appel. Le dispatcher applique le budget retourné (investigation_tools.py:873-876). C’est un risque de comptabilité du budget local par invocation. En revanche, le budget VT persisté des snapshots C demeure bien à 72. Il faut tester ces deux comptabilités séparément.
- **Refus de réutilisation d’un claim terminé.** Le coordinateur refuse correctement une nouvelle occurrence d’un claim terminé avec effect_occurrence_required et preuve EXISTING_CLAIM (external_effects.py:878-912). Le traducteur VT ne classe comme refus pré-envoi que PREPARATION/PRE_CONSUME (investigation_vt_tools.py:537-554) ; ce cas peut donc remonter en NEEDS_REVIEW. C’est une mauvaise actionnabilité pour un non-envoi connu, mais aucune trace C ne prouve que ce garde a été touché. Garder l’interdiction de réutilisation/rejeu ; améliorer seulement le statut du refus connu et prouvé sans envoi.

Signal de sécurité à vérifier séparément : la porte VT de l’application rejette TLP AMBER et RED (investigation_vt_tools.py:1858-1862), tandis que le contrôle durable d’autorité examiné compare explicitement seulement RED (repositories/external_effects.py:1068-1075). Une validation de disclosure source ultérieure peut couvrir AMBER, mais ce chemin n’a pas été établi dans cet audit. Ce possible écart n’explique pas les refus observés, dont le TLP enregistré est Clear. Ajouter un test offline du chemin coordinateur AMBER avant toute modification de garde.

## Diff minimal proposé (conceptuel, non appliqué)

Conserver les gardes G2, TLP, révocation, budget, références de source, claims/CAS et idempotence. Le changement minimal est d’aligner la préparation du pivot, la réponse opérateur et la couverture :

    # investigation_service.py / résolution VT
    refs = trusted_refs_for_exact_pivot(investigation_id, kind, value)
    # Pour MD5/SHA-1, utiliser uniquement une référence canonique vérifiée
    # qui lie la valeur exacte à une source/observation de cette investigation.
    # Si cette provenance n’existe pas, retourner source_references_required.

    # investigation_vt_tools.py : refus prouvé, compteurs d’envoi et de réemploi nuls
    _result(..., framework_pre_submission_proof=..., zero_cost=True)
    # Ne pas mettre zero_cost pour une issue ambiguë, un envoi ou un réemploi.

    # operator_tool_invocation.py : code d’erreur borné et sûr
    output.error.code = safe_tool_result_error_code or status_code
    # Le message doit indiquer l’action : provenance manquante, claim terminé,
    # TLP, révocation, budget, etc. Ne pas exposer l’exception brute ni les valeurs IOC.

    # ToolCoverageEntryV1 / contexte opérateur
    argument_key = canonical_json(tool_name, complete_normalized_arguments)
    # Afficher pour chaque pivot : statut, observation liée ou « aucune »,
    # et code de refus sûr. Ne pas fusionner contacted_ips/contacted_domains.

Le résolveur ne doit accepter aucune référence fournie librement par le modèle ou l’appelant : les références MD5/SHA-1 doivent être dérivées et validées côté serveur contre l’investigation, la cible exacte et les règles de disclosure. Si elles ne peuvent pas être prouvées, le pivot doit apparaître comme bloqué avec le motif actionnable, plutôt que comme exécutable. Un refus prouvé sans envoi reste distinct d’un succès et ne devient jamais une observation fournisseur.

## Tests ciblés proposés — non exécutés

- backend/tests/integration/test_virustotal_external_effects_postgres.py : lecture passive autorisée avec une provenance canonique MD5/SHA-1 liée à la cible ; cas sans référence, mauvaise investigation et source restreinte refusés avant envoi/consommation. Inclure TLP AMBER dans le chemin coordinateur.
- backend/tests/test_investigation_pivot_frontier.py : candidat sans provenance visible comme bloqué avec raison ; deux relations du même hash restent distinctes dans l’état et la couverture.
- backend/tests/test_research_context_builder.py : deux relations produisent des clés de couverture distinctes et chaque entrée relie statut et observation, ou indique explicitement l’absence d’observation.
- backend/tests/test_operator_tool_invocation.py : source_references_required est conservé comme code sûr dans la réponse ; aucun détail IOC ou exception brute n’est exposé.
- Tests VT de PRE_CONSUME : preuve pré-envoi + compteurs nuls = zéro coût local ; EXISTING_CLAIM/effect_occurrence_required connu et sans envoi = refus actionnable, sans NEEDS_REVIEW ; preuve manquante ou issue ambiguë garde le comportement conservateur.

## Niveau de certitude et limites

**Vérifié :** HEAD et fichiers d’archive lus ; identifiants/status des cinq refus ; statut de non-envoi, zéro page/route pour C et A ; absence de B VT dans le journal M07c ; quinze recherches intelligence_search réussies ; G2/TLP/fournisseur/révocation/budget persisté autour de C ; chaîne de contrôle de références, classification, masquage du code opérateur et perte de détail de couverture dans le code source.

**Inféré :** source_references_required est la cause la plus probable pour C et A156, au vu des MD5 sans références et du garde. Le motif de a177 reste inconnu. Les défauts de coût pré-envoi et de EXISTING_CLAIM sont des comportements source vérifiés mais ne sont pas établis comme causes des refus listés.

**Non établi :** code interne exact par refus, motif précis de a177, chemin complet de protection TLP AMBER au niveau durable, tests exécutés ou transport live. Aucun test n’a été lancé et aucun changement de code n’a été fait.
