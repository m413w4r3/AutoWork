REFERENCES

SOURCE S1

title: Iran-Nexus TAG-182 Disseminates MarkiRAT Surveillance Tool

url: [https://www.recordedfuture.com/research/nexus-tag182-disseminates-markirat](https://www.recordedfuture.com/research/nexus-tag182-disseminates-markirat?utm_source=chatgpt.com)

publisher: Recorded Future / Insikt Group

published-at: 2026-07-01

role: primary

SOURCE S2

title: TAG-182 Deploys MarkiRAT in Escalating Iranian Surveillance Campaigns

url: [https://radar.certfa.com/en/threats/view/cd624cc7/](https://radar.certfa.com/en/threats/view/cd624cc7/?utm_source=chatgpt.com)

publisher: CERTFA Radar

published-at: 2026-07-01

role: relay

SOURCE S3

title: Iran-linked group caught hiding surveillance tools in fake apps

url: [https://www.techradar.com/vpn/vpn-privacy-security/iran-linked-group-caught-hiding-surveillance-tools-in-fake-apps](https://www.techradar.com/vpn/vpn-privacy-security/iran-linked-group-caught-hiding-surveillance-tools-in-fake-apps?utm_source=chatgpt.com)

publisher: TechRadar

published-at: 2026-07-25

role: relay

EVENT R1

date: 2026-03-07

sources: S1

text: L’infrastructure `yeplayer[.]store` est observée pour la première fois le 7 mars 2026 ; au cours du même mois, Insikt Group identifie un nouvel échantillon lié à l’infrastructure mise à jour de TAG-182 utilisant le leurre « YESHICA YEPlayer », évolution du thème « YESHICA ».

EVENT R2

date: 2026-05-06

sources: S1

text: Une exécution de `YEPlayer.exe` associée à MarkiRAT communique avec `microsotf[.]comi-site[.]website` et transmet des données par requête POST vers `/up/uploadx.php`; l’échange HTTP documenté par Insikt Group est horodaté du 6 mai 2026. Le comportement conserve des recouvrements avec l’ancien MarkiRAT de Ferocious Kitten, notamment l’usage de BITS et du nom de processus trompeur `svehost.exe`.

EVENT R3

date: 2026-05-17

sources: S1

text: Insikt Group observe `starvpn[.]pis2ray[.]online` présentant un faux service « Star Link » destiné à pousser une application leurre ; le domaine s’insère dans l’infrastructure TAG-182 liée aux thèmes VPN et Pis2ray.

EVENT R4

date: 2026-05-26

sources: S1

text: L’accès à l’Internet mondial est partiellement rétabli en Iran. Insikt Group estime que cette reconnexion crée des conditions favorables à une intensification de la surveillance numérique visant les opposants et réseaux anti-gouvernementaux, contexte dans lequel s’inscrit l’activité de TAG-182.

EVENT R5

date: 2026-07-01

sources: S1, S2

text: Insikt Group publie son analyse de TAG-182 et conclut avec une forte probabilité que le cluster diffuse MarkiRAT au moyen de faux VPN, lecteurs multimédias et autres outils afin de surveiller des Iraniens en Iran et hors du pays. Les cibles sont évaluées comme étant principalement situées en Iran ou liées à des mouvements anti-gouvernementaux en Europe et en Amérique du Nord. CERTFA Radar relaie le même jour la campagne, ses IoC et son ciblage de dissidents.

EVENT R6

date: 2026-07-25

sources: S3

text: TechRadar relaie l’enquête en insistant sur l’usage de Pis2ray VPN et YESHICA/YESHICA YEPlayer comme leurres et sur le fait que les cibles évaluées comprennent explicitement les Iraniens établis hors d’Iran. Le média rappelle également que Recorded Future ne rattache TAG-182 à aucune agence iranienne précise.

UNCERTAINTIES

- Aucun avis gouvernemental ou CERT national spécifiquement consacré à TAG-182 ou MarkiRAT n’a été identifié pour la période éditoriale du 1er au 31 juillet 2026 ; CERTFA Radar reprend explicitement le travail d’Insikt Group et ne constitue donc pas une corroboration technique indépendante.

- Le rapprochement avec Ferocious Kitten n’est pas déterministe : Insikt Group relève des recouvrements significatifs de MarkiRAT, notamment les chaînes BITS et certains choix de tradecraft, et juge une relation de plus en plus probable, mais précise que des preuves supplémentaires sont nécessaires pour conclure à un lien organisationnel.

- TAG-182 n’est attribué à aucune organisation iranienne précise. Insikt Group le place avec une forte probabilité dans l’écosystème plus large de surveillance pro-iranien, sans pouvoir déterminer s’il dépend de l’IRGC, du Basij, de FATA, du MOIS ou d’une autre structure.

- La publication primaire qualifie certains leurres de « fake Android applications », mais les artefacts techniques documentés pour MarkiRAT sont notamment des fichiers MSI/DLL/EXE et des mécanismes Windows tels que BITS et `svehost.exe`. La publication ne fournit pas d’analyse technique d’un APK MarkiRAT permettant de résoudre clairement cette incohérence de plateforme.