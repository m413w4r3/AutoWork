"""LLM prompt templates for production workflow."""

from __future__ import annotations

from collections.abc import Sequence

from cti_app.domain.production import ExtractionProfile

REFERENCES_PROMPT_VERSION = "9"

# AW-011 canonical extraction. The archived document is the only source
# material: each prompt is a pure function of that capture and of the requested
# profile, carrying no Subject, run, job, URL or document identity, so one
# content/profile checkpoint stays valid across runs and Subjects. The versions
# are distinct so a single-source and a batch answer never share a checkpoint.
CANONICAL_EXTRACTION_PROMPT_VERSION = "archive-full-v3"
CANONICAL_IOC_RULES_PROMPT_VERSION = "archive-ioc-rules-v3"
CANONICAL_IOC_RULES_BATCH_PROMPT_VERSION = "archive-ioc-rules-batch-v3"
SYNTHESIS_PROMPT_VERSION = "synthesis-draft-v4-editorial-prose"
SYNTHESIS_PROPOSAL_CONTRACT_VERSION = "synthesis-text-blocks-v2-headingless"
SYNTHESIS_WIRE_PARSER_VERSION = "synthesis-text-parser-v2-headingless-diagnostics"

SYNTHESIS_EDITORIAL_CONTRACT_V4 = """EDITORIAL CONTRACT

Write dense, coherent CTI prose in the publication language. For French
publications, write in French except for source names, quotations and exact
technical literals. Preserve names, commands, paths, keys, fields, ports and
formats exactly as documented. Explain what each mechanism does and how the
steps connect; do not replace documented detail with generic phrases. Use only
the supplied evidence blocks; add no facts, dates, identifiers, causal links,
or source details from memory or outside research.

Return the lead first. It is the first synthesis paragraph and must not repeat
the article title or add a second introduction. Continue the narrative in this
progression, combining related observations into substantial paragraphs:
1. context and carefully qualified attribution;
2. campaign, victimology, and infection/execution chain;
3. TTPs and distinctive mechanisms, including persistence, privilege,
   evasion, and anti-analysis when documented;
4. tools and components;
5. C2 protocol, communication structure, and infrastructure when documented;
6. final paragraphs with confidence limits and CTI analyst observations in
   prose, stating the confidence level and its basis where supported, informed
   by the projected, ranked uncertainty observations below.

Do not write section titles, subtitles, lists of uncertainties, or filler.
Section kinds are internal placement anchors only. A paragraph may cite several
evidence handles when they all support its full account. Keep lineage fine
grained: cite every supporting handle and no unrelated handle. State the
epistemic status explicitly in the prose: distinguish a vendor observation,
an adverse claim, independent corroboration, a hypothesis, and an analytical
inference. A reserve that qualifies attribution or causality must qualify the
same passage it concerns, not only the conclusion.

Use reserve and source-pair context to qualify claims. R handles are citeable
only for passages that state the relevant qualification or contradiction; do
not present reserved material as an unqualified established fact. When
composing a cross-source account, cite each handle that supports the combined
paragraph and state what each source does and does not establish.

For every detection pivot, explain the concrete observable, the telemetry
needed to see it, its link to the described mechanism, and its limits. Never
present an invented rule as a published rule. Do not turn a technical value
into an IOC attribution unless the evidence supports that relation. If the
pack lacks detail on a theme, do not invent it or pad the prose; report the
coverage gap only in the optional diagnostics block.

Do not duplicate recommendations or add vague defensive advice. Do not repeat
the reference timeline merely to restate it. Mention dates in the synthesis
only when they explain technical evolution, scope, attribution, or a material
analytical limit. Preserve the source's exact date precision.

Neutral precision examples (generic placeholders only; never reuse their facts
or handles in the subject's synthesis):

@@CLAIM EXAMPLE-1@@
EVIDENCE: HANDLE_A, HANDLE_B
TEXT: L'éditeur A observe `outil-exemple.exe` lancé avec `/mode-exemple` ; la
source B confirme le processus, sans documenter cet argument.

@@CLAIM EXAMPLE-2@@
EVIDENCE: HANDLE_C, RESERVE_A
TEXT: L'éditeur A attribue l'activité à un opérateur ; la réserve RESERVE_A ne relie
pas l'observation indépendante au même événement, donc cette attribution reste
hypothétique.

@@CLAIM EXAMPLE-3@@
EVIDENCE: HANDLE_D
TEXT: La création de `cle-exemple` par `outil-exemple.exe` constitue un pivot
si la télémétrie conserve le nom du processus et l'opération registre ; ce
signal établit le comportement observé, pas l'identité de l'opérateur.

OUTPUT FORMAT
Use only the text-block wire format below. Do not return JSON. Do not write any
text outside these blocks. Do not emit a HEADING field. Sections are optional;
their kind is only a stable internal placement anchor.

@@LEAD@@
@@CLAIM L001@@
EVIDENCE: E001, E002
TEXT: one plain-text synthesis paragraph in the publication language

@@SECTION technical S001@@
@@CLAIM C001@@
EVIDENCE: E003
TEXT: one plain-text synthesis paragraph in the publication language
@@END SECTION@@

An optional diagnostic block may appear after all synthesis blocks. Use it
only for concrete missing-coverage observations, never for publication prose:

@@DIAGNOSTICS@@
MISSING COVERAGE: <theme absent from the supplied evidence>
@@END DIAGNOSTICS@@
"""
EDITORIAL_ENRICHMENT_PROMPT_VERSION = "editorial-enrichment-text-blocks-v5-semantic-annotations"
EDITORIAL_ENRICHMENT_PROPOSAL_CONTRACT_VERSION = "editorial-enrichment-block-contract-v2"
EDITORIAL_ENRICHMENT_WIRE_PARSER_VERSION = "editorial-enrichment-wire-v2-semantic-annotations"
RELEVANCE_CLASSIFIER_PROMPT_VERSION = "subject-relevance-classifier-v1"
RELEVANCE_CLASSIFIER_CONTRACT_VERSION = "subject-relevance-text-blocks-v1"
RELEVANCE_CLASSIFIER_WIRE_PARSER_VERSION = "subject-relevance-wire-v1"
CANONICAL_EXTRACTION_PROMPT_VERSION_BY_PROFILE = {
    ExtractionProfile.FULL: CANONICAL_EXTRACTION_PROMPT_VERSION,
    ExtractionProfile.IOC_RULES: CANONICAL_IOC_RULES_PROMPT_VERSION,
}


# The semantic contract is shared by single-source and batch extraction. The
# compact line-oriented wire format is parsed locally after the raw response is
# archived, so provider output is never required to be JSON.
_Q2_CANONICAL_RULES = """- La capture archivée est votre seule source. N'utilisez aucun lien, autre
  capture ou souvenir pour compléter son contenu.
- Conservez à l'identique les valeurs techniques, les noms de logiciels
  malveillants, acteurs, outils, produits et techniques, ainsi que les règles
  de détection complètes. Ne défanguez pas, ne reformatez pas et ne modifiez
  pas les valeurs techniques. Conservez les littéraux IPv6.
- Copiez chaque extrait de preuve dans sa langue d'origine, sans le traduire
  ni le paraphraser. Tout séparateur ` :: ` présent dans l'extrait fait partie
  de la citation après le premier séparateur.
- Une ligne mal formée ne doit pas supprimer les autres propositions. Ignorez
  l'élément impossible à représenter et continuez.
- Ajoutez au plus cinq incertitudes analytiques en français, et préférez une
  liste vide au bruit. Gardez uniquement les doutes d'attribution, chiffres ou
  dates contradictoires, limites de confiance ou relations non établies par la
  capture. N'indiquez pas l'absence de règles, les limites de types, les
  captures ou segments incomplets, le classement de fichiers ni les limites
  habituelles."""

_Q2_CANONICAL_ARTIFACTS_AND_RULES = """- Utilisez les groupes `IOC <confirmed|contextual> <type>` pour les valeurs
  domain, ip, url, email, hash, filename, filepath ou cve publiées. Choisissez
  `confirmed` si la capture présente la valeur comme IOC ou infrastructure
  malveillante de l'activité décrite; choisissez `contextual` si la valeur est
  pertinente sans être établie comme malveillante. Ignorez les exemples,
  valeurs masquées ou expurgées et espaces réservés. Écrivez chaque ligne ainsi:
  `- <IOC littéral> :: <court contexte en français>`; omettez le contexte s'il
  n'apporte rien.
- Utilisez `RULE <yara|sigma|suricata|snort>[: name]` uniquement pour les règles
  de détection complètes publiées dans la capture. Encadrez leur corps littéral
  dans un bloc Markdown correspondant. Conservez syntaxe et retours à la ligne.
  N'inventez, ne réparez, ne complétez, ne reformatez, n'aplatissez et ne
  fusionnez aucune règle. Ignorez les règles incomplètes.
- Sous `UNCERTAINTIES`, ajoutez au plus cinq incertitudes analytiques en
  français, ou aucune puce."""

_Q2_CANONICAL_OUTPUT_PREAMBLE = """Répondez uniquement dans le format compact en lignes ci-dessous. N'ajoutez
aucun texte autour. Commencez chaque groupe par l'un de ces en-têtes:

FACT <category>
EVENT <YYYY-MM-DD ou date absolue>
IOC <confirmed|contextual> <type>
RULE <yara|sigma|suricata|snort>[: name]
UNCERTAINTIES

Chaque puce FACT ou EVENT contient une description en français, puis ` :: `,
puis un extrait littéral exact de la capture. Chaque puce IOC contient sa
valeur littérale et peut être suivie d'un court contexte français après ` :: `.
Écrivez une puce par élément.

Pour une date publiée précise, utilisez le format ISO `YYYY-MM-DD`. Sinon,
utilisez une formulation absolue incluant l'année, comme `fin 2024` ou `T2
2026`; n'utilisez jamais de date relative comme « la même année ». Gardez
uniquement les événements liés au sujet principal de la publication et
ordonnez-les chronologiquement. Placez chaque règle complète après son en-tête
RULE, dans un bloc Markdown correspondant. Utilisez `EMPTY` uniquement si la
capture ne contient aucune proposition et `UNAVAILABLE` uniquement si elle ne
peut pas être analysée."""

_Q2_CANONICAL_ARCHIVED_SOURCE = """La capture archivée exacte d'une publication CTI est fournie ci-dessous.

Analysez uniquement la capture ci-dessous. Ne naviguez pas sur le Web, ne
suivez aucun lien et ne complétez pas cette source à partir de votre mémoire ou
d'une autre publication. Cette capture est l'unique source de l'extraction.

--- DÉBUT DE LA CAPTURE ARCHIVÉE ---
{source_text}
--- FIN DE LA CAPTURE ARCHIVÉE ---"""


class ProductionPromptTemplates:
    """Versioned prompt templates for subject production."""

    REFERENCES_RESEARCH_V2 = """You are a threat intelligence research assistant. Your task is to conduct web research and build a chronological reference timeline for the following subject:

**Subject**: {subject_title}

**Initial Information**:
{subject_description}

**Actor/Campaign**: {actor_info}

**Technical Data**:
{technical_summary}

**Research Date**: {research_date}

**Editorial Period**: {period_start} to {period_end}

**Core Publications**:
{core_sources}

**Previously Known Supporting References**:
{supporting_sources}

**Research Guidelines**:
1. Prioritize government bodies (CISA, CERT, national agencies)
2. Technical sources from original researchers and security vendors
3. Independent technical analysis and published research
4. Avoid redundant reprints without added value
5. Verify all dates are on or before the research date
6. Use only publicly available information
7. When a relevant publication links directly to an IOC list, technical
   appendix/annex, technical indicator list, YARA/Sigma/Suricata content, a
   malware sandbox/report page, a downloadable IOC TXT/CSV, a vendor IOC page,
   or its associated official technical repository/file (including a GitHub
   repository/file) containing directly linked technical material for the same
   incident or campaign, add that URL as a distinct SOURCE. Only add it when
   the resource concerns the same subject. Do not turn every hyperlink into a
   SOURCE; do not add generic download or database pages, navigation, marketing,
   generic documentation, or unrelated reports.
8. Core publications define the central editorial subject. Preserve every
   relevant accessible core publication in the resulting SOURCE set.
9. Web research is additive. New references supplement or corroborate core
   publications; they do not replace them.
10. Supporting references may add chronology, attribution, technical details,
    IOC context, annexes and corroboration.
11. Write event text and uncertainties in French. Include only events that are
    part of the subject's main activity; omit unrelated historical context.
12. Sort events strictly by date, oldest first. Use an absolute date: ISO
    `YYYY-MM-DD` when precise, otherwise an absolute wording with its year such
    as `fin 2024`, `T2 2026`, or `février 2025`. Never use relative dates such
    as “the same year” or “also that year”.
13. Write at most five analytical uncertainties in French. Include only
    doubtful attribution, contradictory figures or dates, confidence limits,
    or an unestablished relationship. Omit routine limitations and banalities.
14. Keep source provenance and editorial authority separate. `role` records
    whether the publisher is primary, independent, a relay, an aggregator, a
    social account, or unknown. `editorial-role` records how this source should
    contribute: `primary` for the core publication, `corroboration` for an
    independent account, `context` for contextual material or a technical
    annex, and `counter-analysis` for material that qualifies or contradicts a
    substantive claim. A technical resource that provides only an IOC/rule
    annex is `context`; a technical resource with its own relevant analysis is
    `corroboration` or `counter-analysis`.

**Output format** — plain Markdown, no code fence, no JSON:

# REFERENCES

editorial-title: <titre français au format [Acteur principal] Titre, ou [Publication] Titre>

## SOURCE S1

title: <title>
url: https://...
publisher: <publisher>
published-at: YYYY-MM-DD
role: primary|independent|relay|aggregator|social|unknown
editorial-role: primary|corroboration|context|counter-analysis
kind: publication|technical_resource
reason: <short explanation of relevance to the Subject>

## EVENT R1

date: YYYY-MM-DD or absolute date wording with year
sources: S1, S2
text: <one chronological event, in French>

# UNCERTAINTIES
- <incertitude analytique en français, ou omit the section>

Rules:
- Produce the editorial title during this references step.
- Keep S1, S2, ... and R1, R2, ... as compact transport aliases only; they
  are not canonical identifiers and must never be presented as such.
- Every event must cite at least one source alias defined above; never cite an
  alias you did not define.
- No date after the research date.
"""

    CANONICAL_TECHNICAL_EXTRACTION_V1 = (
        """Vous analysez une publication CTI archivée afin d'en extraire des
propositions réutilisables centrées sur cette source.

"""
        + _Q2_CANONICAL_ARCHIVED_SOURCE
        + """

"""
        + _Q2_CANONICAL_OUTPUT_PREAMBLE
        + """

- Utilisez les groupes FACT pour les faits durables que AW-012 peut réutiliser.
  Rédigez chaque description en français, puis ` :: `, puis l'extrait exact
  dans la langue d'origine de la capture. Choisissez une seule catégorie parmi
  actors,
  campaigns, malware, tools, products, infection_chain,
  ttps, victimology, protocols, infrastructure, files, commands, persistence,
  detections, sectors, countries, other_technical. Conservez un identifiant
  MITRE ATT&CK littéral uniquement si la capture le mentionne. Ne reformulez
  pas le texte de l'article sans l'étayer par son extrait.
- Utilisez EVENT uniquement pour la chronologie du sujet principal décrit dans
  la publication. Écrivez le texte de l'événement en français, puis ` :: `,
  puis son extrait exact. Incluez la date publiée dans l'extrait si elle est
  indiquée. Mettez une date précise au format ISO dans l'en-tête; sinon,
  utilisez une date absolue normalisée avec son année, comme `fin 2024` ou
  `T2 2026`. N'utilisez pas de date relative, n'estimez et n'inventez aucune
  date. Triez les événements du plus ancien au plus récent.
"""
        + _Q2_CANONICAL_ARTIFACTS_AND_RULES
        + """

"""
        + _Q2_CANONICAL_RULES
    )

    CANONICAL_IOC_RULES_EXTRACTION_V1 = (
        """Vous analysez une publication CTI archivée afin d'en extraire les
indicateurs techniques et règles de détection publiés qui peuvent être réutilisés.

"""
        + _Q2_CANONICAL_ARCHIVED_SOURCE
        + """

Ce profil ne produit aucune proposition narrative. N'utilisez pas de groupes
FACT ou EVENT.

"""
        + _Q2_CANONICAL_OUTPUT_PREAMBLE
        + """

"""
        + _Q2_CANONICAL_ARTIFACTS_AND_RULES
        + """

"""
        + _Q2_CANONICAL_RULES
    )

    CANONICAL_IOC_RULES_BATCH_EXTRACTION_V1 = (
        """Vous analysez plusieurs publications CTI archivées et indépendantes en une fois.

Chaque capture ci-dessous est repérée par une étiquette temporaire de la forme
`@@Q2:B<nombre>@@`, où `<nombre>` est la suite de chiffres de l'étiquette
fournie. Ces étiquettes ne servent qu'au transport de cette réponse: elles ne
désignent pas les sources et ne sortent jamais de cette réponse.

Analysez chaque capture séparément et attribuez chaque proposition à la capture
qui la contient littéralement. Ne déplacez aucun IOC ni aucune règle d'une
capture à l'autre, n'utilisez pas une capture pour interpréter une autre et
n'inférez aucun contenu absent de la capture concernée.

Ne naviguez pas sur le Web et ne suivez aucun lien.

Ce profil ne produit aucune proposition narrative. N'utilisez pas de groupes
FACT ou EVENT dans les blocs de source.

Répondez uniquement par un bloc de source pour chaque étiquette fournie.
Recopiez chaque étiquette à l'identique et remplacez `<nombre>` par ses chiffres:

@@Q2:B<nombre>@@
IOC confirmed domain
- evil.example :: infrastructure de commande et de contrôle

Dans chaque bloc, utilisez le même format IOC_RULES que pour une source seule:
groupes IOC, règles RULE complètes et au plus cinq incertitudes analytiques en
français. N'omettez et ne combinez aucune étiquette. Gardez chaque proposition
dans le bloc de la capture qui la contient littéralement.
Si une capture ne contient aucune proposition, écrivez `EMPTY` dans son bloc.

"""
        + _Q2_CANONICAL_ARTIFACTS_AND_RULES
        + """

"""
        + _Q2_CANONICAL_RULES
        + """

Captures archivées:

{batch_sources}
"""
    )

    @classmethod
    def get_references_prompt(
        cls,
        subject_title: str,
        subject_description: str,
        actor_info: str,
        technical_summary: str,
        research_date: str,
        period_start: str,
        period_end: str,
        core_sources_text: str,
        supporting_sources_text: str,
    ) -> str:
        return cls.REFERENCES_RESEARCH_V2.format(
            subject_title=subject_title,
            subject_description=subject_description,
            actor_info=actor_info,
            technical_summary=technical_summary,
            research_date=research_date,
            period_start=period_start,
            period_end=period_end,
            core_sources=core_sources_text or "- None supplied.",
            supporting_sources=supporting_sources_text or "- None supplied.",
        )

    @classmethod
    def get_canonical_archive_extraction_prompt(
        cls,
        source_text: str,
        *,
        profile: ExtractionProfile = ExtractionProfile.FULL,
    ) -> str:
        """Render the AW-011 canonical contract for one archived capture.

        The archived text is the only variable input: the prompt never carries
        a Subject, run, job, URL or document identity, so the same content and
        profile always render the same prompt for every caller.
        """
        if not source_text.strip():
            raise ValueError("A canonical extraction prompt requires archived source text")
        template = (
            cls.CANONICAL_TECHNICAL_EXTRACTION_V1
            if profile is ExtractionProfile.FULL
            else cls.CANONICAL_IOC_RULES_EXTRACTION_V1
        )
        return template.format(source_text=source_text)

    @classmethod
    def get_canonical_archive_batch_prompt(
        cls,
        batch_sources: Sequence[tuple[str, str]],
    ) -> str:
        """Render the AW-011 IOC_RULES batch over archived captures.

        Each entry is a ``(batch_id, archived_text)`` pair. The local handles
        are temporary transport labels; no URL or document identity is sent.
        """
        blocks = []
        for batch_id, source_text in batch_sources:
            if not batch_id.strip() or not source_text.strip():
                raise ValueError("A canonical batch entry requires a handle and text")
            blocks.append(
                f"@@Q2:{batch_id}@@\n"
                "--- DÉBUT DE LA CAPTURE ARCHIVÉE ---\n"
                f"{source_text}\n"
                "--- FIN DE LA CAPTURE ARCHIVÉE ---"
            )
        if not blocks:
            raise ValueError("A canonical batch prompt requires at least one source")
        return cls.CANONICAL_IOC_RULES_BATCH_EXTRACTION_V1.format(batch_sources="\n\n".join(blocks))
