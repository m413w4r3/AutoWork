from __future__ import annotations

from cti_app.application.discovery.contracts import DiscoverEditionParameters

# ruff: noqa: RUF001 - The exact French business prompt intentionally uses typographic apostrophes.

PROMPT_TEMPLATE_ID = "monthly-cti-discovery"
PROMPT_TEMPLATE_VERSION = "4.1"


def _research_prompt(parameters: DiscoverEditionParameters) -> str:
    aliases: list[str] = []
    seen_aliases = {parameters.country.strip().casefold()}
    for value in parameters.country_aliases:
        alias = value.strip()
        fingerprint = alias.casefold()
        if alias and fingerprint not in seen_aliases:
            aliases.append(alias)
            seen_aliases.add(fingerprint)
    formatted_aliases = f" (alias : {', '.join(aliases)})" if aliases else ""
    languages: list[str] = []
    seen_languages: set[str] = set()
    for value in parameters.languages:
        language = value.strip()
        fingerprint = language.casefold()
        if language and fingerprint not in seen_languages:
            languages.append(language)
            seen_languages.add(fingerprint)
    observable_end = min(parameters.period_end, parameters.as_of_date)
    return f"""Mission : rechercher les publications CTI significatives concernant
{parameters.country}{formatted_aliases}.

Date de recherche : {parameters.as_of_date.isoformat()}
Période demandée : {parameters.period_start.isoformat()} au {parameters.period_end.isoformat()}
Période observable : {parameters.period_start.isoformat()} au {observable_end.isoformat()}
Langues de travail de l'édition : {", ".join(languages)}
Axe complémentaire : {parameters.complementary_axis}

La langue n'est jamais un critère de sélection. Recherche dans toutes les
langues et n'écarte aucune publication au motif qu'elle n'est pas rédigée dans
une langue de travail de l'édition. Couvre notamment l'anglais, le français,
l'espagnol, le portugais, l'allemand, l'italien, le néerlandais, le polonais,
l'ukrainien, le russe, le turc, l'arabe, le persan, l'hébreu, le chinois,
le japonais, le coréen, le vietnamien, l'indonésien, le thaï et l'hindi,
qui concentrent une part importante de la production CTI publique.

Conserve le titre exact de chaque publication dans sa langue d'origine, sans le
traduire ni le translittérer. Les champs de description que tu rédiges restent
en français.

Ne recherche pas de publication postérieure à la date de recherche.

Priorise les activités APT étatiques ou supposées étatiques et les publications
techniques comportant des IOC, des échantillons, des configurations, une chaîne
d’infection, des outils, des TTP ou des règles de détection.

Propose tous les sujets significatifs retrouvés. Il n’existe aucune limite ni
quota de sujets, de brèves ou d’articles approfondis. La sélection finale sera
effectuée par un analyste humain.

Regroupe dans un même SUBJECT les publications décrivant manifestement la même
campagne, le même incident ou la même recherche.

Une synthèse mensuelle ou trimestrielle peut être liée à plusieurs SUBJECT.
Ne fusionne pas des campagnes différentes uniquement parce qu’elles sont
mentionnées dans la même synthèse.

Chaque SUBJECT doit normalement comporter au moins une publication dans la
période observable. Les publications antérieures peuvent être ajoutées comme
rapport original, analyse indépendante ou contexte technique.

Limite cette phase à la sélection éditoriale. N’effectue pas encore l’analyse
exhaustive de la chaîne d’infection, des TTP, des outils ou de la victimologie.

Pour les IOC :

- signale uniquement les IOC explicitement visibles dans les pages consultées ;
- reproduis leurs valeurs exactes sans les corriger ni les compléter ;
- indique leur type lorsqu’il est identifiable ;
- distingue un total annoncé par l’éditeur des valeurs effectivement visibles ;
- n’estime jamais un nombre d’IOC ;
- utilise `unknown` si tu ne peux pas déterminer l’information ;
- utilise `none` seulement si la publication indique clairement qu’aucun IOC
  n’est fourni ou si son contenu visible permet de l’établir ;
- une URL normale de publication ou de navigation n’est pas un IOC ;
- un domaine d’éditeur ou de CDN n’est pas un IOC sauf s’il est explicitement
  présenté comme tel dans la source.

N’invente aucune URL, date, attribution, disponibilité d’artefact ou valeur
d’IOC.

Retourne uniquement du Markdown, sans bloc de code et sans texte avant le titre.
N’échappe pas les tirets des noms de champs.
N’insère pas de citation Markdown dans les champs de description.
Toutes les URL de référence doivent apparaître dans un bloc PUBLICATION.

# SUJETS CANDIDATS

## SUBJECT S1

title: <intitulé proposé>
presentation: <deux phrases neutres maximum>
actor-campaign: <acteur ou campagne explicitement rapporté, sinon unknown>
technical-potential: <entier de 0 à 4>
technical-reason: <raison en une phrase>
artifacts: <liste parmi ioc, samples, configurations, pcap, yara, suricata, none, unknown>
uncertainty: <une ou deux incertitudes courtes>

### PUBLICATION P1

title: <titre exact>
url: <URL HTTP(S) exacte>
publisher: <éditeur ou unknown>
published-at: <YYYY-MM-DD ou unknown>
role: <primary, independent, relay, aggregator ou unknown>
ioc-visibility: <none, declared, visible ou unknown>
visible-ioc-types: <liste des types visibles ou none/unknown>
visible-iocs: <jusqu’à 10 valeurs exactes explicitement visibles ou none/unknown>
publisher-ioc-count: <entier explicitement annoncé ou unknown>
ioc-note: <une phrase courte ou none>

### PUBLICATION P2

...

## SUBJECT S2

...

# LIMITES

<limites principales de la recherche et de l’accès aux sources>"""
