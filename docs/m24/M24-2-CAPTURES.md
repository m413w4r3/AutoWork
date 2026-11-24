# Captures M24.2

Revue visuelle du parcours navigateur E4, sur données synthétiques et adaptateurs
Bridge, VirusTotal, Shodan, Codex et Claude factices. Les captures d’interface
font 1280×800, 1440×900 et 1920×1080. Les pages PDF sont les sorties Typst
rastérisées par `pdftoppm`.

Chaque chemin M24.2 ci-dessous a été ouvert avec l’outil de lecture d’images.
Le chemin M24.1 indique le comparatif visuel le plus proche; `—` signifie que
M24.1 ne montrait pas cet état ou ce contenu.

| Sujet | Avant — M24.1 | Après — capture M24.2 | Observation sur la capture ouverte |
|---|---|---|---|
| Overview actif 1280×800 | [cockpit-overview](captures-m24-1/cockpit-overview-1280x800.png) | [overview-active-1280x800.png](captures-m24-2/overview-active-1280x800.png) | Les quatre tuiles sont alignées; le cockpit montre 31 tâches actives et « À traiter maintenant ». |
| Overview actif 1440×900 | [cockpit-overview](captures-m24-1/cockpit-overview-1440x900.png) | [overview-active-1440x900.png](captures-m24-2/overview-active-1440x900.png) | Les tuiles gardent une largeur régulière et l’action G2 se lit sans chevauchement. |
| Overview actif 1920×1080 | [cockpit-overview](captures-m24-1/cockpit-overview-1920x1080.png) | [overview-active-1920x1080.png](captures-m24-2/overview-active-1920x1080.png) | La progression IOC, VT et tâches s’ajoute sous les indicateurs dans une grille ample. |
| Décision en attente 1280×800 | — | [overview-decision-pending-1280x800.png](captures-m24-2/overview-decision-pending-1280x800.png) | Les compteurs affichent 2 décisions requises et l’action « Examiner une tâche » est visible. |
| Décision en attente 1440×900 | — | [overview-decision-pending-1440x900.png](captures-m24-2/overview-decision-pending-1440x900.png) | L’état de revue et l’extension G2 restent distincts dans la liste d’actions. |
| Décision en attente 1920×1080 | — | [overview-decision-pending-1920x1080.png](captures-m24-2/overview-decision-pending-1920x1080.png) | Les tuiles, les deux actions et la progression tiennent ensemble avec une hiérarchie nette. |
| Plan et liste 30 tâches 1280×800 | [task-card](captures-m24-1/task-card-1280x800.png) | [plan-task-list-30-1280x800.png](captures-m24-2/plan-task-list-30-1280x800.png) | La colonne de liste montre plusieurs tâches synthétiques; le détail reste lisible dans la colonne droite. |
| Plan et liste 30 tâches 1440×900 | [task-card](captures-m24-1/task-card-1440x900.png) | [plan-task-list-30-1440x900.png](captures-m24-2/plan-task-list-30-1440x900.png) | Les cartes et le résumé de tâche sont séparés par une gouttière stable. |
| Plan et liste 30 tâches 1920×1080 | [task-card](captures-m24-1/task-card-1920x1080.png) | [plan-task-list-30-1920x1080.png](captures-m24-2/plan-task-list-30-1920x1080.png) | La liste affiche davantage de cartes sans réduire la largeur du panneau de détail. |
| Détail Codex 1280×800 | [task-card](captures-m24-1/task-card-1280x800.png) | [task-detail-codex-1280x800.png](captures-m24-2/task-detail-codex-1280x800.png) | Le badge « En cours », le contexte et la session Codex sont reconnaissables; le bas du panneau est sous le pli. |
| Détail Codex 1440×900 | [task-card](captures-m24-1/task-card-1440x900.png) | [task-detail-codex-1440x900.png](captures-m24-2/task-detail-codex-1440x900.png) | Les états de tâche et de session sont lisibles au-dessus du formulaire d’instruction. |
| Détail Codex 1920×1080 | [task-card](captures-m24-1/task-card-1920x1080.png) | [task-detail-codex-1920x1080.png](captures-m24-2/task-detail-codex-1920x1080.png) | La session, les actions de sortie et le transfert restent visibles dans une colonne large. |
| Détail Claude 1280×800 | [task-card-claude](captures-m24-1/task-card-claude-1280x800.png) | [task-detail-claude-1280x800.png](captures-m24-2/task-detail-claude-1280x800.png) | Le fournisseur Claude et l’état « En cours » sont clairement différenciés du contenu de la tâche. |
| Détail Claude 1440×900 | [task-card-claude](captures-m24-1/task-card-claude-1440x900.png) | [task-detail-claude-1440x900.png](captures-m24-2/task-detail-claude-1440x900.png) | Le panneau agent garde les mêmes alignements que le panneau Codex. |
| Détail Claude 1920×1080 | [task-card-claude](captures-m24-1/task-card-claude-1920x1080.png) | [task-detail-claude-1920x1080.png](captures-m24-2/task-detail-claude-1920x1080.png) | Le résumé, l’instruction et les actions sont visibles sans densité excessive. |
| Instruction en file 1280×800 | [operators-panel](captures-m24-1/operators-panel-1280x800.png) | [instruction-en-file-1280x800.png](captures-m24-2/instruction-en-file-1280x800.png) | Le badge « En file » et la position persistante distinguent l’instruction non transmise. |
| Instruction en file 1440×900 | [operators-panel](captures-m24-1/operators-panel-1440x900.png) | [instruction-en-file-1440x900.png](captures-m24-2/instruction-en-file-1440x900.png) | Le texte de l’instruction tient sur la ligne d’état et le formulaire reste séparé. |
| Instruction en file 1920×1080 | [operators-panel](captures-m24-1/operators-panel-1920x1080.png) | [instruction-en-file-1920x1080.png](captures-m24-2/instruction-en-file-1920x1080.png) | La tâche, son état et l’instruction en file sont lisibles avec les actions du runner. |
| Instruction transmise 1280×800 | [operators-panel](captures-m24-1/operators-panel-1280x800.png) | [instruction-transmise-1280x800.png](captures-m24-2/instruction-transmise-1280x800.png) | « Transmise » apparaît séparément du statut de session « En cours ». |
| Instruction transmise 1440×900 | [operators-panel](captures-m24-1/operators-panel-1440x900.png) | [instruction-transmise-1440x900.png](captures-m24-2/instruction-transmise-1440x900.png) | Le libellé et le texte restent lisibles; le formulaire vide n’empiète pas sur l’état livré. |
| Instruction transmise 1920×1080 | [operators-panel](captures-m24-1/operators-panel-1920x1080.png) | [instruction-transmise-1920x1080.png](captures-m24-2/instruction-transmise-1920x1080.png) | L’état transmis et la prochaine exécution sont visibles dans un panneau aéré. |
| Instruction acquittée 1280×800 | [operators-panel](captures-m24-1/operators-panel-1280x800.png) | [instruction-acquittee-1280x800.png](captures-m24-2/instruction-acquittee-1280x800.png) | La coche et le badge « Acquittée » rendent l’accusé visible malgré le panneau compact. |
| Instruction acquittée 1440×900 | [operators-panel](captures-m24-1/operators-panel-1440x900.png) | [instruction-acquittee-1440x900.png](captures-m24-2/instruction-acquittee-1440x900.png) | L’acquittement est lisible au-dessus des détails techniques repliés. |
| Instruction acquittée 1920×1080 | [operators-panel](captures-m24-1/operators-panel-1920x1080.png) | [instruction-acquittee-1920x1080.png](captures-m24-2/instruction-acquittee-1920x1080.png) | Les trois niveaux de livraison sont distingués par des badges et une coche. |
| Workspace, note ouverte 1280×800 | [workspace-notes](captures-m24-1/workspace-notes-1280x800.png) | [workspace-note-open-1280x800.png](captures-m24-2/workspace-note-open-1280x800.png) | Le texte édité est lisible avec les actions du workspace; la carte de provenance continue sous le pli. |
| Workspace, note ouverte 1440×900 | [workspace-notes](captures-m24-1/workspace-notes-1440x900.png) | [workspace-note-open-1440x900.png](captures-m24-2/workspace-note-open-1440x900.png) | La note, ses badges et l’avertissement de non-preuve sont alignés dans deux colonnes. |
| Workspace, note ouverte 1920×1080 | [workspace-notes](captures-m24-1/workspace-notes-1920x1080.png) | [workspace-note-open-1920x1080.png](captures-m24-2/workspace-note-open-1920x1080.png) | Le contenu, les actions et les sections de provenance restent visibles sans chevauchement. |
| Ancre d’article sélectionnée 1280×800 | [anchor-picker](captures-m24-1/anchor-picker-1280x800.png) | [article-anchor-selected-1280x800.png](captures-m24-2/article-anchor-selected-1280x800.png) | Le paragraphe ciblé est encadré en bleu; le contexte Bridge est lisible dans la colonne latérale. |
| Ancre d’article sélectionnée 1440×900 | [anchor-picker](captures-m24-1/anchor-picker-1440x900.png) | [article-anchor-selected-1440x900.png](captures-m24-2/article-anchor-selected-1440x900.png) | La sélection visuelle et les occurrences du paragraphe restent distinctes. |
| Ancre d’article sélectionnée 1920×1080 | [anchor-picker](captures-m24-1/anchor-picker-1920x1080.png) | [article-anchor-selected-1920x1080.png](captures-m24-2/article-anchor-selected-1920x1080.png) | La colonne d’article et la recherche de provenance coexistent; la liste latérale est dense. |
| Proposition Bridge 1280×800 | [anchor-picker](captures-m24-1/anchor-picker-1280x800.png) | [bridge-proposal-1280x800.png](captures-m24-2/bridge-proposal-1280x800.png) | Le texte proposé et les limites Bridge sont lisibles, avec une partie du contexte au-dessus du cadrage. |
| Proposition Bridge 1440×900 | [anchor-picker](captures-m24-1/anchor-picker-1440x900.png) | [bridge-proposal-1440x900.png](captures-m24-2/bridge-proposal-1440x900.png) | L’aperçu à l’emplacement précède la décision; le contenu anglais synthétique reste lisible. |
| Proposition Bridge 1920×1080 | [anchor-picker](captures-m24-1/anchor-picker-1920x1080.png) | [bridge-proposal-1920x1080.png](captures-m24-2/bridge-proposal-1920x1080.png) | Le statut en attente, la confirmation et les boutons Accepter/Correction/Refuser sont visibles. |
| Diff de correction 1280×800 | [note-review](captures-m24-1/note-review-1280x800.png) | [bridge-correction-diff-1280x800.png](captures-m24-2/bridge-correction-diff-1280x800.png) | Les suppressions rouges sont reconnaissables; les ajouts sont en partie sous le pli. |
| Diff de correction 1440×900 | [note-review](captures-m24-1/note-review-1440x900.png) | [bridge-correction-diff-1440x900.png](captures-m24-2/bridge-correction-diff-1440x900.png) | Les deux lignes supprimées et les deux lignes ajoutées sont séparées par couleur et soulignement. |
| Diff de correction 1920×1080 | [note-review](captures-m24-1/note-review-1920x1080.png) | [bridge-correction-diff-1920x1080.png](captures-m24-2/bridge-correction-diff-1920x1080.png) | Le diff complet est lisible dans une zone large avec les sources citées dessous. |
| Référence M23 1280×800 | [anchor-picker](captures-m24-1/anchor-picker-1280x800.png) | [m23-element-selected-1280x800.png](captures-m24-2/m23-element-selected-1280x800.png) | Le sélecteur d’élément M23 et les observations canoniques sont présents, mais la moitié gauche reste vide. |
| Référence M23 1440×900 | [anchor-picker](captures-m24-1/anchor-picker-1440x900.png) | [m23-element-selected-1440x900.png](captures-m24-2/m23-element-selected-1440x900.png) | Les identifiants d’observation s’empilent dans le formulaire latéral et se lisent difficilement. |
| Référence M23 1920×1080 | [anchor-picker](captures-m24-1/anchor-picker-1920x1080.png) | [m23-element-selected-1920x1080.png](captures-m24-2/m23-element-selected-1920x1080.png) | Le sélecteur tient dans la page, mais les lignes techniques restent longues et denses. |
| Tableau IOC 1280×800 | [anchor-picker](captures-m24-1/anchor-picker-1280x800.png) | [ioc-table-selected-1280x800.png](captures-m24-2/ioc-table-selected-1280x800.png) | La longue liste de sources est visible; le texte technique est difficile à parcourir rapidement. |
| Tableau IOC 1440×900 | [anchor-picker](captures-m24-1/anchor-picker-1440x900.png) | [ioc-table-selected-1440x900.png](captures-m24-2/ioc-table-selected-1440x900.png) | La liste reste dense et plusieurs identifiants se prolongent jusqu’au bord du panneau. |
| Tableau IOC 1920×1080 | [anchor-picker](captures-m24-1/anchor-picker-1920x1080.png) | [ioc-table-selected-1920x1080.png](captures-m24-2/ioc-table-selected-1920x1080.png) | La sélection source reste dans sa colonne; les lignes d’observation dominent visuellement. |
| Proposition avec blocs 1280×800 | [note-review](captures-m24-1/note-review-1280x800.png) | [bridge-proposal-with-blocks-1280x800.png](captures-m24-2/bridge-proposal-with-blocks-1280x800.png) | La liste de référence annonce la figure, le tableau IOC et l’extrait canonique. |
| Proposition avec blocs 1440×900 | [note-review](captures-m24-1/note-review-1440x900.png) | [bridge-proposal-with-blocks-1440x900.png](captures-m24-2/bridge-proposal-with-blocks-1440x900.png) | Le tableau IOC est rendu en colonnes avec en-têtes distincts et provenance repliable. |
| Proposition avec blocs 1920×1080 | [note-review](captures-m24-1/note-review-1920x1080.png) | [bridge-proposal-with-blocks-1920x1080.png](captures-m24-2/bridge-proposal-with-blocks-1920x1080.png) | Figure, tableau et extrait de code sont visibles; le contenu JSON technique est très dense. |
| Sélecteur de code canonique 1280×800 | [anchor-picker](captures-m24-1/anchor-picker-1280x800.png) | [canonical-code-selector-1280x800.png](captures-m24-2/canonical-code-selector-1280x800.png) | La source et la fenêtre d’octets sont visibles; la sélection bleue masque une partie de l’extrait. |
| Sélecteur de code canonique 1440×900 | [anchor-picker](captures-m24-1/anchor-picker-1440x900.png) | [canonical-code-selector-1440x900.png](captures-m24-2/canonical-code-selector-1440x900.png) | Les commandes Texte/Hexadécimal et « Vérifier et référencer » sont lisibles; le repli manuel reste fermé. |
| Sélecteur de code canonique 1920×1080 | [anchor-picker](captures-m24-1/anchor-picker-1280x800.png) | [canonical-code-selector-1920x1080.png](captures-m24-2/canonical-code-selector-1920x1080.png) | Les octets, le langage et les références sont visibles, dans une colonne droite étroite face à une grande zone vide. |
| Aperçu PDF 1280×800 | [pdf-preview](captures-m24-1/pdf-preview-1280x800.png) | [pdf-preview-1280x800.png](captures-m24-2/pdf-preview-1280x800.png) | « PDF prêt » et le repli Ouvrir/Télécharger sont visibles; le navigateur headless n’intègre pas le lecteur. |
| Aperçu PDF 1440×900 | [pdf-preview](captures-m24-1/pdf-preview-1440x900.png) | [pdf-preview-1440x900.png](captures-m24-2/pdf-preview-1440x900.png) | Le statut, la version et la taille du PDF sont regroupés au-dessus des actions. |
| Aperçu PDF 1920×1080 | [pdf-preview](captures-m24-1/pdf-preview-1920x1080.png) | [pdf-preview-1920x1080.png](captures-m24-2/pdf-preview-1920x1080.png) | L’état du document est net; l’espace blanc du lecteur reste visible en mode headless. |
| PDF page 1 827×1170 | [pdf-page-1](captures-m24-1/pdf-page-1.png) | [pdf-page-1-827x1170.png](captures-m24-2/pdf-page-1-827x1170.png) | La première page montre l’article, le tableau M23, la note corrigée et la figure ancrée. |
| PDF page 2 827×1170 | — | [pdf-page-2-827x1170.png](captures-m24-2/pdf-page-2-827x1170.png) | La note, le tableau IOC et l’extrait de code restent dans la même zone de page; la petite typographie technique est dense. |
| PDF page 3 827×1170 | — | [pdf-page-3-827x1170.png](captures-m24-2/pdf-page-3-827x1170.png) | L’annexe IOC tient sur une page, avec libellés de valeur, observation, source et SHA alignés. |
| G2 révoqué 1280×800 | [overview-revoked](captures-m24-1/overview-revoked-1280x800.png) | [overview-revoked-1280x800.png](captures-m24-2/overview-revoked-1280x800.png) | L’alerte révoquée et les droits VT/Bridge/Shodan interdits sont visibles; le haut du cockpit est hors cadre. |
| G2 révoqué 1440×900 | [overview-revoked](captures-m24-1/overview-revoked-1440x900.png) | [overview-revoked-1440x900.png](captures-m24-2/overview-revoked-1440x900.png) | Le bandeau jaune et les droits interdits se distinguent; le panneau budget commence sous le pli. |
| G2 révoqué 1920×1080 | [overview-revoked](captures-m24-1/overview-revoked-1920x1080.png) | [overview-revoked-1920x1080.png](captures-m24-2/overview-revoked-1920x1080.png) | Le bandeau, les droits coupés et les budgets consommés sont visibles dans la même zone. |

## Défauts visuels restants

- Les panneaux de sélection M23/IOC et de code canonique consacrent beaucoup de
  largeur à la colonne latérale; la zone d’article paraît vide sur ces captures.
- Les listes de sources canoniques affichent encore des lignes techniques
  longues et denses, moins faciles à balayer que les libellés regroupés de la
  liste de tâches.
- À 1280 px, plusieurs détails de tâche ou de proposition continuent sous le
  pli. Le contenu reste accessible par défilement; la capture ne montre pas
  toute la page.
- Le lecteur PDF intégré n’est pas rendu par Chromium headless. Le repli de
  téléchargement/ouverture est visible et les trois pages réelles ont été
  vérifiées séparément après rasterisation.
- Les exemples de note et les extraits de rapports sont synthétiques et
  conservent quelques libellés anglais de fixture.

Le contrôle `python3 scripts/check_no_binary_fixtures.py
docs/m24/captures-m24-2` a réussi: le script n’interdit que les suffixes
exécutables/binaires dans le répertoire des fixtures de test; les PNG de cette
documentation sont admis.
