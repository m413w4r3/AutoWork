# Atelier éditorial — spécification UX

Document de conception pour ARTICLE et BRIEF. Les opérations visibles dépendent des capacités et des opérations autorisées par le snapshot serveur. Le serveur reste autorité pour l’identité des blocs, les preuves, les versions et l’adoption des propositions. Les chaînes marquées « proposées » sont du texte UX à mapper aux codes d’erreur stables, pas une définition de comportement backend.

## 1. Principes

1. Le document est le centre : la cible se choisit dans le texte, jamais dans un formulaire séparé.
2. Une sélection met ensemble bloc, contexte, preuves, opérations permises et discussion liée à l’occurrence.
3. Modifier, discuter, insérer, comparer et consulter les sources restent accessibles au bloc actif.
4. Les brouillons et propositions ne changent pas le canonique avant validation humaine ; aucun parcours ne perd son contexte.
5. Le rendu du document et du PDF doit refléter les mêmes contenus et placements confirmés.

## 2. Cadres et responsive

Les dimensions ci-dessous sont des largeurs de colonnes indicatives en pixels CSS ; les gouttières s’ajoutent. La zone de lecture du document reste entre 680 et 860 px quand l’espace le permet.

### ≥ 1440 px — trois colonnes

~~~text
┌──────────────────────────────────────────────────────────────────────────┐
│ Barre persistante : Retour · Sujet · ARTICLE/BRIEF · vN · État · PDF · ⋯ │
├──────────────┬──────────────────────────────────────┬────────────────────┤
│ PLAN         │ DOCUMENT                             │ INSPECTEUR         │
│ 250 px       │ 760 px de lecture                    │ 380 px             │
│ ≈18 %        │ ≈55 %                                │ ≈27 %              │
│ sticky       │ titre, chapeau, sections, enrichis   │ scroll indépendant│
│ recherche    │ dans l’ordre réel de publication     │ composer en bas    │
└──────────────┴──────────────────────────────────────┴────────────────────┘
~~~

Le plan contient titre, chapeau, sections hiérarchiques, recherche de titres et paragraphes, section active et indicateurs discrets de brouillon/proposition. Un clic dans le plan fait défiler jusqu’au bloc et le sélectionne. Le document domine visuellement ; l’inspecteur mesure 360–420 px et défile indépendamment.

### 1024–1439 px — plan repliable

~~~text
┌──────────────────────────────────────────────────────────────────┐
│ Barre persistante · Plan [ouvrir] · ARTICLE/BRIEF · vN · PDF · ⋯ │
├────┬─────────────────────────────────┬───────────────────────────┤
│Plan│ DOCUMENT                        │ INSPECTEUR                │
│56px│ 58–65 % de la zone utile        │ 35–42 %, min. ≈340 px    │
│rail│ texte ≤860 px, sans cartes       │ onglets + composer fixe  │
└────┴─────────────────────────────────┴───────────────────────────┘
~~~

Le plan se replie en rail ou s’ouvre en panneau. L’inspecteur reste latéral ; le texte conserve une largeur lisible.

### < 1024 px — document prioritaire

~~~text
┌─────────────────────────────────┐
│ Retour · Sujet · vN · État · ⋯  │
│ Plan     DOCUMENT      Inspecteur│
├─────────────────────────────────┤
│ DOCUMENT pleine largeur         │
│ bloc sélectionné                │
│                    [Inspecteur] │
└─────────────────────────────────┘
~~~

Le document occupe la largeur disponible. Plan et inspecteur s’ouvrent par commandes explicites ; l’inspecteur apparaît en panneau latéral superposé sans redimensionner ni perdre la position du document. Un tableau large défile dans son propre conteneur, jamais sur toute la page.

### < 768 px — inspecteur en feuille plein écran

~~~text
┌──────────────────────────────┐
│ ‹ Retour au document         │
│ Discussion sur : [cible]     │
│ onglets                       │
│ fil, comparaison ou sources   │
│                               │
│ composer ancré en bas         │
└──────────────────────────────┘
~~~

La feuille remplace temporairement le document à l’écran. « Retour au document » est toujours visible, restaure le bloc et le défilement d’origine, et n’abandonne aucun brouillon. Il n’y a pas de défilement horizontal de page.

## 3. Barre supérieure et états document

Barre persistante : retour au sujet, titre abrégé, type « Article » ou « Brève », numéro de version canonique, état français, bouton « Voir le PDF », menu « Historique » et « Détails techniques ». Les UUID, hashes, IDs d’artefact et diagnostics restent dans les détails avancés ; aucun chapelet de badges colorés.

Chaque état est du texte visible et possède la description accessible ci-dessous (nom accessible ou texte associé). La couleur ne porte jamais seule l’information.

| Libellé | Description accessible |
|---|---|
| À jour | « Le document affiché correspond à la dernière version canonique enregistrée. » |
| Modification en cours | « Une modification est en cours de validation ; le document affiché est encore la version enregistrée. » |
| Brouillon local | « Un brouillon de ce bloc n’est pas encore enregistré comme nouvelle version canonique. » |
| Conflit | « La version a changé depuis le début de cette modification ; aucun changement concurrent n’a été écrasé. » |
| PDF obsolète | « Le PDF correspond à une version antérieure du document. Régénérez l’aperçu pour consulter la version actuelle. » |

Chargement, absence de contenu produit, version périmée, erreur de lecture et lecture seule (format non éditable) ont chacun un état textuel avec une action sûre. Aucun indicateur de chargement ne tourne indéfiniment.

## 4. Blocs, sélection et actions

### États visibles d’un bloc

| État | Présentation et actions |
|---|---|
| Normal | Typographie de lecture, sans cadre de carte. Les actions apparaissent au focus clavier comme au survol. |
| Survol | Repère d’interaction léger et actions du bloc ; ne masque ni ne sélectionne le texte. |
| Sélectionné | Repère persistant discret, barre d’actions, titre de cible dans l’inspecteur ; contexte, preuves et fil de l’occurrence se mettent à jour. |
| Édition | Champ inline adapté au bloc, focus et curseur visibles, texte sélectionnable/copier/collable ; boutons « Enregistrer » et « Annuler ». Aucun contentEditable global. |
| Brouillon | Badge texte « Brouillon local » près du bloc et dans le plan ; valeur gardée par bloc, action « Reprendre le brouillon ». Le changement de cible ne l’efface pas. |
| Proposition en attente | Carte distincte dans le fil, libellée « Proposition » ; contenu proposé, cible et version de référence visibles ; document canonique inchangé ; « Comparer », « Accepter », « Rejeter », « Demander une variante », « Consulter les preuves ». |
| Conflit | Bandeau explicatif, ancienne base et version courante identifiées sans jargon ; aucune adoption ni écrasement ; actions de comparaison/actualisation et nouvelle proposition selon les capacités. |
| Enregistré | Confirmation « Enregistré · vN » ; le bloc reste sélectionné, le fil et la position restent en place, l’inspecteur ne se réinitialise pas. |

Un clic sur un paragraphe ou titre synchronise sélection, section, preuves, opérations et discussion. Le texte reste sélectionnable ; liens, copie et navigation clavier gardent leur comportement natif. Les actions non autorisées sont masquées, ou désactivées avec une raison lisible si leur absence serait incompréhensible.

### Barre d’actions et menus « Plus »

Les actions principales sont « Modifier », « Discuter », « Ajouter après », « Sources » et « Plus », limitées aux opérations permises par le snapshot. Le menu conserve le libellé de la cible et se ferme sans déplacer la sélection.

| Type de bloc | Actions directes | Menu « Plus » (si autorisé) |
|---|---|---|
| Titre | « Renommer », « Demander un autre titre », « Comparer », « Historique » | Historique et opérations supplémentaires déclarées par le snapshot. |
| Chapeau (lead) | « Modifier », « Discuter », « Sources », « Comparer » | Historique ; autres opérations déclarées. |
| Intertitre (heading) | « Renommer », « Discuter », « Sources », « Historique » | Déplacer ou supprimer uniquement si proposés par les capacités ; l’identité de section est conservée. |
| Paragraphe | « Modifier », « Discuter », « Ajouter après », « Sources » | « Supprimer », « Scinder », « Déplacer », « Historique » uniquement si l’opération est permise et prise en charge. |
| Tableau | « Sélectionner », « Modifier » (structure/cellules si permis), « Sources » | « Historique », retrait/déplacement si opérations permises ; « Ajouter après » si disponible. |
| Graphique | « Sources », « Modifier les libellés ou la présentation » si permis | « Historique », retrait/déplacement si opérations permises ; ajout après si disponible. |
| Figure | « Sources », légende si modifiable, provenance | « Historique », « Remplacer » ou « Retirer » seulement via opération versionnée autorisée. |

Pour un diagramme présent, montrer la provenance et l’historique, puis n’exposer que les opérations déclarées pour son kind. L’atelier ne déduit jamais une capacité à partir du texte ni d’un contrôle client.

### ARTICLE et BRIEF

Les deux types utilisent le même atelier, les mêmes états et la même navigation ; le type reste visible dans la barre. Le snapshot pilote les opérations permises. Pour BRIEF, la galerie applique le plafond de trois figures et indique le nombre déjà utilisé ; aucun autre plafond n’est supposé. Les limites sont expliquées avant l’action, sans mode « article » par défaut ni changement silencieux de type.

## 5. Inspecteur, discussion et Bridge

### Onglets et priorité

Onglets : « Discussion », « Modification directe », « Insertion », « Sources et preuves », « Historique ». La Discussion prend la priorité d’affichage dès qu’un fil est actif pour le bloc : sélectionner un autre onglet n’archive ni ne remplace le fil, et revenir au bloc le restaure. Lors d’une réponse asynchrone, ne pas détourner le focus ni remplacer un autre onglet. L’en-tête indique « Discussion sur : [début du paragraphe] » (ou le titre du bloc) et « Changer de cible ».

L’historique est chronologique et différencie messages utilisateur, réponses Bridge, propositions, décisions et refus de preuve. Il rappelle la version de référence d’un fil conservé. Le contexte envoyé est celui du bloc, de sa section, des preuves admises, des passages adjacents utiles, du fil et des règles éditoriales ; jamais tout le sujet par défaut. Les sorties et messages ne deviennent pas des preuves.

### Composer

- Zone multiligne avec placeholder « Que souhaitez-vous modifier ou clarifier ? » ; « Entrée » envoie, « Maj+Entrée » insère un saut de ligne.
- Deux commandes explicitement distinctes : « Envoyer un message » (discussion, ne crée pas de proposition) et « Générer une proposition » (demande une proposition à comparer). Le mode est visible avant envoi ; les raccourcis n’acceptent jamais une proposition.
- Les actions rapides ouvrent le mode proposition et préremplissent une consigne modifiable. Suggestions : « Raccourcir », « Clarifier », « Ajouter du détail technique », « Améliorer le style », « Réorganiser ». Elles insèrent du texte dans le composer, sans soumission automatique.
- L’envoi possiblement reçu ne peut pas être renvoyé à l’aveugle. Une seule génération Bridge est active à la fois ; aucune commande « Réessayer » ne resoumet un état incertain. La vérification/réconciliation lit l’envoi associé ; elle ne le recrée pas.

### États Bridge — libellé et copie d’état

Libellés prescrits par le cadrage ; descriptions proposées pour que l’état reste compréhensible sans couleur.

| État | Texte visible exact | Action sûre |
|---|---|---|
| Prêt | « La discussion est prête. Choisissez “Envoyer un message” ou “Générer une proposition”. » | Composer selon le mode choisi. |
| Appel en attente | « Votre demande attend qu’une capacité Bridge soit disponible. Son état sera suivi dans ce fil. » | Attendre dans le fil ; ne pas créer un doublon. |
| Génération en cours | « Bridge traite votre demande. L’état sera mis à jour dans ce fil. » | Attendre ; aucune resoumission. |
| Réponse disponible | « Une réponse est disponible dans ce fil. Consultez-la avant toute décision. » | Lire ; si proposition, comparer avant de décider. |
| Appel refusé | « Bridge a refusé la demande. Consultez le motif ; aucune modification canonique n’a été appliquée. » | Lire le motif et les preuves ; poursuivre par une action autorisée. |
| Session indisponible | « Cette session Bridge est perdue. Ouvrez un nouveau fil pour poursuivre. » | Nouveau fil ; jamais d’URL de session externe. |
| Réconciliation nécessaire | « L’état de votre envoi est incertain. Vérifiez son état dans ce fil avant de continuer ; la demande ne sera pas renvoyée automatiquement. » | « Vérifier l’état de l’envoi » ; lecture de l’envoi associé uniquement. |

### Erreurs : copies visibles

Les erreurs sont en français, associées à un code stable côté serveur. Les deux premières copies sont prescrites par le cadrage ; les autres sont des propositions de microcopie à associer au code correspondant. Garder le motif reçu, ne pas afficher un code nu.

| Cas | Message exact et action |
|---|---|
| Version périmée / fence manquée | « Cette proposition repose sur une version qui a changé. Elle n’a pas été appliquée. Actualisez le document, puis comparez et formulez une nouvelle proposition. » Actions : actualiser et comparer ; ne jamais écraser. |
| Fait non étayé | « Cette nouvelle valeur technique n'est pas étayée par les preuves disponibles. Modifiez le passage ou ajoutez une source autorisée. » |
| Pas de données quantitatives | « Aucun graphique quantitatif vérifiable ne peut être créé à partir des preuves actuelles. Un tableau descriptif peut être proposé à la place. » Aucune valeur n’est inventée ou extrapolée. |
| Figure non éligible | « Cette figure ne peut pas être intégrée : [motif d’exclusion fourni]. Choisissez une figure éligible. » La case reste non cochable. |
| Session perdue | « Cette session Bridge est perdue. Ouvrez un nouveau fil pour poursuivre. » Le fil courant n’est pas réutilisé ; aucune URL externe. |
| Réconciliation requise | « L’état de votre envoi est incertain. Vérifiez son état dans ce fil avant de continuer ; la demande ne sera pas renvoyée automatiquement. » Bouton : « Vérifier l’état de l’envoi », jamais « Réessayer ». |

## 6. Parcours clés et budget de clics

Budget = nombre maximal de clics/taps sur les commandes ; saisie, chargement et réponse asynchrone ne comptent pas. Un clic sur une action de comparaison ou de décision compte. Les raccourcis clavier restent équivalents.

1. **Renommer le titre avec Bridge — budget 5.** Cliquer le titre → « Demander un autre titre »/« Discuter » → saisir la consigne → « Générer une proposition » → lire « Titre actuel / Titre proposé / Justification » en Comparaison → « Accepter ». Jusqu’à confirmation, le titre canonique ne change pas ; après acceptation, le titre et la version affichés s’actualisent sans quitter le document.
2. **Réécrire un paragraphe en trois tours — budget 9.** Cliquer le paragraphe → « Discuter » → consigne et « Générer une proposition » (tour 1) → « Demander une variante », saisir « Plus court » et générer (tour 2) → choisir la suggestion « Je préfère la deuxième » qui référence explicitement la deuxième proposition affichée, puis générer (tour 3) → comparer → accepter la proposition voulue. L’ordinal est lié à son UUID/segments par l’interface ; il n’est jamais envoyé seul. Le fil reste ouvert et le bloc conserve son identité.
3. **Éditer à la main et garder un brouillon au changement de bloc — budget 5.** Cliquer le premier bloc → « Modifier » → saisir → cliquer un second bloc : le premier devient « Brouillon local » → revenir au premier via document/plan → « Reprendre le brouillon » puis « Enregistrer ». Aucune génération Bridge n’a lieu ; annuler ne supprime que le brouillon actif.
4. **Créer un tableau — budget 5.** Sélectionner le paragraphe d’ancrage → « Ajouter après » → « Tableau » → vérifier/modifier la consigne préremplie et générer la proposition → examiner le tableau structuré, ajuster colonnes/intitulés/cellules → « Accepter ». Le tableau confirmé apparaît après l’ancre et au même emplacement dans le PDF.
5. **Créer un graphique ou refuser faute de données — budget 6.** Sélectionner l’ancre → « Ajouter après » → « Graphique » → choisir « Barres » ou « Chronologie » → générer → vérifier titre, légende, valeurs, preuves et avertissements → accepter. Si aucune donnée vérifiable n’est disponible, afficher le refus exact ci-dessus, sans proposition chiffrée inventée ; suggérer le tableau descriptif lorsque les données le permettent.
6. **Galerie et sélection multiple — budget 6.** Sélectionner l’emplacement → « Ajouter après » → « Image issue des sources » → filtrer/rechercher → cocher deux figures éligibles → vérifier « 2 images sélectionnées », leur légende, ordre et emplacement → « Intégrer les images sélectionnées ». L’aperçu détaillé n’altère pas la sélection. Aucun appel modèle ; provenance visible après insertion. Pour BRIEF, le compteur bloque tout choix au-delà de trois figures.
7. **Conflit entre deux sessions — budget 7.** Session A : sélectionner le bloc, générer une proposition et la laisser ouverte. Session B : sélectionner le même bloc, modifier puis enregistrer. Revenir à A et cliquer « Accepter » : afficher « Conflit », refuser l’adoption périmée et préserver le contenu de B ; actualiser, comparer les versions et créer une nouvelle proposition si souhaité. Aucun écrasement silencieux.
8. **PDF obsolète puis régénération — budget 3.** Après une modification confirmée, cliquer « Voir le PDF » → constater l’état « PDF obsolète » et la version concernée → cliquer « Régénérer l’aperçu » → quand l’état est à jour, ouvrir le PDF. Aucune repin automatique d’édition.
9. **Parcours mobile — budget 5.** À moins de 768 px, sélectionner un paragraphe → « Discuter » ouvre la feuille plein écran → saisir la consigne et « Générer une proposition » → comparer/accepter → « Retour au document ». La feuille restitue le paragraphe sélectionné et sa position ; composer et boutons restent visibles sans scroll horizontal.

## 7. Comparaison et différences

- « Comparaison » montre « Version actuelle » et « Proposition » côte à côte sur grand écran, empilées sur écran étroit. Le paragraphe complet reste lisible ; la justification et les preuves sont accessibles près de la proposition.
- « Différences » affiche ajouts et suppressions dans le texte, au niveau du paragraphe ; jamais un diff JSON. Marqueurs, libellés (« Ajouté », « Supprimé ») et couleur distinguent les changements, même en monochrome ou lecteur d’écran.
- Les deux vues comparent la même cible et la même proposition sélectionnée. Les variantes précédentes sont navigables dans le fil ; choisir « la deuxième » rattache explicitement la référence à la proposition correspondante.
- Les commandes « Accepter », « Rejeter », « Demander une variante » et « Consulter les preuves » restent visibles et nommées. Accepter ne rappelle pas le modèle ; une proposition stale ne peut pas être acceptée.

## 8. Galerie des figures

Fenêtre/panneau dédié : en-tête avec recherche, filtres « Article/source » et « Statut », compteurs « Disponibles » et « Sélectionnées ». Grille de deux à quatre colonnes selon l’espace, miniatures progressives et bornées. Chaque vignette montre miniature, nom/légende, source, statut, case à cocher et mention « Déjà utilisée » si applicable.

L’aperçu détaillé présente image, source exacte, contexte textuel, page/section, dimensions, légende originale, statut et motif d’exclusion ; l’ouvrir ne modifie pas les cases cochées. Un panneau de sélection permet retrait, légende éditoriale, ordre et emplacement (paragraphe courant par défaut), puis « Intégrer les images sélectionnées ». Aucun modèle ni URL distante pour miniature/rendu ; la provenance reste consultable après insertion.

États visibles : « Chargement de la galerie », « Aucune figure disponible », « Disponible », « Sélectionnée », « Non retenue », « Non éligible », « Fichier indisponible », « Figure non vérifiée », « Déjà intégrée » et « Erreur de miniature ». Les non-éligibles ne sont pas cochables ; chaque exclusion affiche son motif. Au chargement progressif, garder la place et le nom de la figure. Conserver les validations archivées et le plafond de trois figures de BRIEF.

## 9. Clavier et accessibilité

- **Entrée / Maj+Entrée :** envoyer / saut de ligne dans le composer. **Ctrl/Cmd+Entrée :** soumettre une action seulement si l’action courante et son raccourci sont annoncés. **Ctrl/Cmd+S :** enregistrer l’édition manuelle. Ne pas intercepter les raccourcis natifs des champs.
- **Échap :** fermer menu, feuille ou éditeur ; demander confirmation si la fermeture abandonnerait une saisie. **Tab / Maj+Tab :** ordre logique ; flèches dans le plan pour parcourir les entrées. Chaque action est accessible sans survol.
- Après acceptation, laisser le focus sur le bloc mis à jour (ou son titre) et garder la sélection. À la fermeture d’une feuille/dialogue, rendre le focus au contrôle qui l’a ouvert. Après erreur, placer/annoncer le focus au message ou au premier contrôle utile sans sauter la page en haut.
- Annoncer les changements d’état et réponses asynchrones dans une région `aria-live="polite"`; réserver `role="alert"` aux erreurs bloquantes. Ne pas annoncer chaque poll ni déplacer le focus à l’arrivée d’une réponse.
- Cibles tactiles d’au moins 44 × 44 px ; focus visible ; noms accessibles pour icônes ; structure de titres, libellés et tables sémantique ; contraste WCAG 2.2 AA ; ajouts/suppressions différenciés sans dépendre de la couleur.
- Respecter `prefers-reduced-motion`; transitions discrètes, pas de défilement brutal vers une réponse. Aucun défilement horizontal de page sur mobile.

## 10. Hiérarchie visuelle

Réutiliser les conventions de `frontend/src/styles.css` : police Inter/système, titres éditoriaux en Georgia si utile, boutons `.button` et `.button--secondary`, surfaces claires et bordures fines. Le CSS global ne fournit pas de système de variables dédié : ne pas inventer une palette parallèle.

Ordre de lecture : 1) document, 2) bloc actif, 3) proposition, 4) actions contextuelles, 5) preuves et détails techniques. Repères typographiques : texte 16 px avec interligne 1,5–1,7 ; titre 26–32 px ; intertitres 18–24 px ; métadonnées 12–14 px ; longueur de ligne confortable. Séparer les paragraphes par l’espace plutôt que par des cartes imbriquées.

Palette sobre de la feuille de style : texte #17211d, fond #eef2ed, action principale verte #1f6042, texte secondaire #5b6b62, bordures #cfdbd3/#d4ded7. Réserver les tons verts à l’action ou la sélection, les tons d’erreur au blocage, et les autres accents aux changements du diff ou états demandant une intervention. Toujours joindre libellé/icône/forme au code couleur. Maintenir des contrôles nets, un rayon de bordure modéré, et des transitions réduites selon les préférences système.

## 11. Checklist d’acceptation UX-01…UX-20

- **UX-01** — Clic paragraphe : le bloc, la section, les actions et les preuves visibles correspondent à l’occurrence sélectionnée.
- **UX-02** — Titre : une proposition se compare et s’accepte ; le titre et la version changent ensuite, avec l’état PDF correspondant.
- **UX-03** — Trois tours : « Je préfère la deuxième » désigne une proposition précise ; après acceptation, le fil reste accessible.
- **UX-04** — Rejet : le document canonique et sa version restent identiques.
- **UX-05** — Édition manuelle : nouvelle version après validation et aucune requête Bridge observée.
- **UX-06** — Changement de bloc pendant la saisie : le premier brouillon reste visible dans le plan et se reprend sans perte.
- **UX-07** — Deux paragraphes de même texte restent sélectionnables et adressables comme occurrences distinctes.
- **UX-08** — Deux sessions : une proposition fondée sur l’ancienne version est refusée, le contenu concurrent reste intact et le conflit est expliqué.
- **UX-09** — Tableau ancré après le troisième paragraphe : même emplacement dans l’atelier et dans le PDF rendu.
- **UX-10** — Graphique étayé : titre, valeurs, légende, preuves et rendu sont consultables avant/après adoption.
- **UX-11** — Données insuffisantes : le texte de refus apparaît et aucune valeur ou proposition quantitative n’est inventée.
- **UX-12** — Galerie : filtrer, sélectionner deux figures, intégrer ; assets et provenance sont visibles après confirmation.
- **UX-13** — Figure non éligible : case inactive et motif d’exclusion lisible dans la vignette et l’aperçu.
- **UX-14** — BRIEF : mêmes parcours que ARTICLE, capacités respectées et insertion bloquée au plafond de trois figures.
- **UX-15** — Bridge indisponible/incertain : message explicite, reprise sûre ou nouveau fil ; aucune resoumission ambiguë.
- **UX-16** — PDF obsolète : version périmée indiquée, régénération accessible, puis aperçu à jour consultable.
- **UX-17** — Une version de publication devenue périmée est signalée ; aucune repin automatique d’édition n’a lieu.
- **UX-18** — Clavier : sélectionner, ouvrir les onglets, modifier, comparer, décider et fermer sans souris ; focus restauré.
- **UX-19** — Mobile : feuille inspecteur plein écran, retour au bloc et aucune barre de défilement horizontale de page.
- **UX-20** — Ancienne version : affichage historique explicite en lecture seule ; aucune migration implicite ni réécriture de blob.
