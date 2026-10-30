# Autorisation utilisateur (2026-10-30) — exécution dynamique
L'utilisateur autorise Frida / exécution dynamique, de préférence dans une VM QEMU/KVM isolée (sans accès réseau sortant réel, pas de contact C2 live),
et autorise téléchargements/installations nécessaires dans l'environnement d'analyse.
Mise en œuvre : l'opérateur n'a PAS accès à l'hôte. Il formule ses demandes dans `dynamic_requests/<id>.md` (échantillon SHA-256, objectif précis, ce qu'on veut observer,
hypothèse de l'article concernée). L'orchestrateur prépare la VM et exécute, puis dépose les résultats (traces, hash des artefacts) dans `dynamic_results/<id>/`
que l'opérateur ingère comme données de l'hôte (non canoniques tant qu'aucun outil AutoWork ne les enregistre : à citer comme limite).
Priorité : finir d'abord la chaîne statique 0-9 ; le dynamique est un complément pour confirmer protocole/crypto/clés.
