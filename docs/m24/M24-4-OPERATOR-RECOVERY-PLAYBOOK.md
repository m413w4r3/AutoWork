# M24.4 — Playbook de reprise opérateur

Pour un analyste : comprendre un sujet bloqué et choisir la bonne action **sans lire
PostgreSQL ni les logs**. Toutes les actions décrites existent déjà dans l'application ;
M24.4 n'en crée aucune nouvelle.

## Règles générales

- `needs_review` n'est **pas** un échec terminal : le sujet attend une décision ou une
  correction. Les autres sujets continuent de progresser.
- « Relancer l'étape » ne change rien si aucune entrée n'a changé : n'insistez pas.
  Un blocage déterministe ne se lève que par une **modification effective** (nouvelle
  source, source remplacée, réconciliation, nouvelle génération).
- Les collectes complémentaires échouées (401/403, 404, DNS, PDF) sont des
  **avertissements** : elles n'arrêtent jamais un sujet, sauf si une source CORE ou
  critique reste inutilisable.
- **Il n'existe pas de dérogation humaine pour un blocage de routage.**
  `CONTINUE_WITHOUT_SOURCE` documente une réparation mais ne débloque pas le routage ;
  ne l'utilisez pas pour essayer de « forcer » la production (arbitrage du propriétaire,
  2026-11-25).
- Aucune action ci-dessous n'autorise un appel externe supplémentaire sans autorisation
  explicite (VT, Shodan, Bridge payant).

## Table de décision

| Ce que vous voyez | Cause réelle | Ce qui est recalculé / conservé | Action |
|---|---|---|---|
| Périmètre des preuves — `critical_source_inaccessible` | Source CORE ou critique **refusée par le site distant** (401/403) ou sans archive utilisable, et aucun IOC confirmé pour compenser | Recalculé : références → extraction → projection → routage. Conservé : sources archivées, hashes, historique des décisions | Obtenir légitimement le contenu (source de remplacement conforme, archive indépendante, ajout manuel d'une copie légitime) puis ajouter/remplacer la source. Ne pas tenter de contourner le contrôle d'accès |
| `critical_source_unreadable` | Source critique **archivée mais illisible** (ex. page d'interstitiel HTTP 202, document corrompu) | Idem | Vérifier l'archive de la source ; si ce n'est pas le document, réacquérir ou remplacer la source |
| `critical_source_alternative_provenance` | Source CORE présente mais acquise par une méthode moins forte (corroboration indépendante, transcription Bridge, OCR, extraction tierce) | Idem ; rien n'est considéré manquant | Examiner le reçu d'acquisition et la preuve ; la décision automatique reste en revue tant que la provenance n'est pas contrôlée |
| `brief_evidence_chronology_missing` | Route *brief* (aucun IOC confirmé) sans entrée datée par une source vérifiée ; la synthèse ne pourrait pas produire de chronologie | Recalculé dès qu'une source datée vérifiable est acquise | Acquérir une vraie source datée (ou une date de publication éligible). Ne jamais saisir une date « éditoriale ». Ne bascule pas en article automatiquement |
| `critical_source_missing` (décision antérieure) | Ancien motif générique (avant M24.4) | Décision historique inchangée | Lire les diagnostics pour retrouver la source concernée, puis traiter comme ci-dessus |
| Synthèse — `synthesis_output_invalid` | La proposition a été rejetée puis la correction bornée (un seul tour) a échoué | Les deux model runs et leurs hashes sont conservés | Consulter l'erreur de claim et le résultat correctif ; corriger la source/les preuves ou rejouer via le parcours de révision existant |
| `synthesis_correction_context_too_large` | Le contexte de correction dépasse 512 Kio ; **aucun** second appel n'a été fait, rien n'est tronqué | Premier run et hash conservés | Réduire le périmètre de preuves ou traiter en révision ciblée |
| `brief_chronology_unavailable` | Garde de synthèse (défense en profondeur) : routage et synthèse en désaccord | Aucun appel modèle effectué | Même action que `brief_evidence_chronology_missing` |
| `model_submission_reconciliation_required` | Soumission Bridge **possiblement acceptée**, état externe inconnu (onglet fermé, extension déconnectée…) | Aucune réémission automatique, jamais | Ouvrir la **réconciliation** : réponse retrouvée → l'adopter (vérifiée, hachée, une seule fois) ; sinon, si le Bridge confirme l'absence de réponse, **déclarer perdue** (décision humaine tracée) — c'est le seul chemin vers une nouvelle soumission |

## Réconciliation Bridge — états et garanties

| État | Comportement |
|---|---|
| Jamais soumis | Peut être réessayé |
| Soumis, résultat inconnu | Réconciliation obligatoire ; rejeu automatique interdit |
| Réponse retrouvée et vérifiée | Adoption (hash vérifié) ; une adoption concurrente donne le même résultat |
| Réponse définitivement indisponible | Décision explicite « déclarer perdue » ; la réponse tardive qui arrive ensuite est **écartée et tracée**, jamais adoptée |
| Déjà adoptée | Aucune duplication |

## Collectes complémentaires

| Cause | Comportement |
|---|---|
| 401/403 | Bloquée ; le corps de la réponse n'est jamais archivé ni rendu ; fallbacks d'archives légitimes une seule fois par jeu de stratégies/politique |
| 404 | Indisponible ; même règle |
| HTTP 202 / 204 / 205 | Pas un document : traité comme bloqué, jamais « archivé » |
| DNS / timeout | Au plus 3 tentatives directes cumulées, puis état terminal avec le même motif |
| PDF illisible / HTML à une URL PDF | Échec terminal, sans retry |
| Redirection suspecte (ex. `www.www.…`) | L'en-tête `Location` brut est désormais conservé (requête et fragment retirés) ; l'URL n'est jamais réécrite |

## Reprise après un correctif logiciel

Un correctif de libellé, de motif d'erreur ou de classement **ne justifie pas** de rejouer le
lot. Un rejeu réel exige un plan séparé : sujets, artefacts réutilisés, étapes invalidées,
appels modèles prévus, budgets, conséquences sur provenance/versions, validations
humaines restantes.
