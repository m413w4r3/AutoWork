/**
 * Human sentence for the timeline's terse server summary.
 *
 * The canonical timeline projection keeps a machine token as `summary`
 * (`kind:value`, e.g. `vt.download_sample:needs_review` or
 * `pivot_approve:pivot`) because it is built for every surface at once. The
 * cockpit translates it for the analyst; the raw token stays available as the
 * item's `title`. Unknown tokens are rendered verbatim: the projection is the
 * authority, so a new backend value must surface instead of being swallowed.
 */

import type { TimelineItemV1 } from "../api/tasks";
import type {
  AgentSessionState,
  InstructionState,
  OperatorAdapterKind,
  TaskEventType,
  TaskStatus,
  ToolInvocationStatus,
} from "../api/types";
import type { WorkContributionImportStatus } from "../api/contributions";
import { EVIDENCE_KIND_LABELS } from "../opinion/labels";
import { INVOCATION_STATUS_LABELS } from "../samples/samplesLabels";
import {
  ADAPTER_LABELS,
  INSTRUCTION_JOURNAL_STATE_LABELS,
  SESSION_STATE_LABELS,
  TASK_ACTOR_LABELS,
  TASK_EVENT_LABELS,
  TASK_STATUS_LABELS,
} from "./labels";

const DECISION_TYPE_LABELS: Record<string, string> = {
  member_validate: "Validation d’un membre",
  member_reject: "Rejet d’un membre",
  feature_validate: "Validation d’une fonctionnalité",
  feature_reject: "Rejet d’une fonctionnalité",
  pivot_approve: "Approbation d’un pivot",
  pivot_reject: "Rejet d’un pivot",
  note_approve: "Approbation d’une note",
  note_changes_requested: "Modifications demandées sur une note",
  detection_approve: "Approbation d’une détection",
  detection_reject: "Rejet d’une détection",
  plan_accept: "Acceptation du plan",
  plan_reject: "Rejet du plan",
  task_result_accept: "Acceptation d’un résultat de tâche",
  task_result_changes_requested:
    "Modifications demandées sur un résultat de tâche",
  workspace_conflict_resolve: "Résolution d’un conflit de workspace",
};

const DECISION_TARGET_LABELS: Record<string, string> = {
  member: "membre",
  feature: "fonctionnalité",
  tool: "outil",
  invariant: "invariant",
  pivot: "pivot",
  corpus: "corpus",
  detection: "détection",
  note: "note",
  plan_version: "version de plan",
  task: "tâche",
  workspace_conflict: "conflit de workspace",
};

/** Observation object types the investigation tools persist today. */
const OBJECT_TYPE_LABELS: Record<string, string> = {
  file: "fichier",
  sample: "échantillon",
  binary: "binaire",
  host: "hôte",
  ip: "adresse IP",
  domain: "domaine",
  url: "URL",
  network_capture: "capture réseau",
  native_analysis: "analyse native",
  apk_analysis: "analyse APK",
  dotnet_analysis: "analyse .NET",
  investigation_bootstrap_snapshot: "instantané de démarrage de l’enquête",
  protocol_model: "modèle de protocole",
  family_feature_comparison: "comparaison de famille",
  tool_result: "résultat d’outil",
  advisor_call: "appel Bridge",
  advisor_response_received: "réponse Bridge",
  virustotal_search: "recherche VirusTotal",
  detection_candidate_version: "version de règle candidate",
  detection_candidate_comparison: "comparaison de règles candidates",
  article_claim_citation: "citation de revendication de l’article",
  manual_rule_corpus_import: "import manuel de corpus de règles",
  vt_retrohunt_candidate_hit: "résultat de rétrohunt VT",
  provider_authorization_refusal: "refus d’autorisation fournisseur",
  provider_download_route_refusal: "refus de route de téléchargement",
  provider_download_authorization_refused:
    "refus d’autorisation de téléchargement",
};

const TOOL_LABELS: Record<string, string> = {
  "advisor.consult": "Consultation Bridge",
  "article.get_claims": "Lecture des revendications de l’article",
  "evidence.get": "Lecture d’une preuve",
  "protocol.validate": "Validation d’un protocole",
  "sample.analyze": "Analyse d’un échantillon",
  "sample.set_qualification": "Qualification d’un échantillon",
  "vt.behaviour": "Comportements VT",
  "vt.download_sample": "Téléchargement d’un échantillon VT",
  "vt.file_report": "Rapport VT d’un fichier",
  "vt.object_report": "Rapport VT d’un objet",
  "vt.relationships": "Relations VT",
  "vt.search": "Recherche VT",
};

const PLAN_PARSE_LABELS: Record<string, string> = {
  parsed: "analysé",
  partially_parsed: "partiellement analysé",
};

const CONTRIBUTION_LABELS: Record<string, string> = {
  "work_contribution:imported": "Contribution de travail importée",
  "work_contribution:rejected": "Contribution de travail refusée",
  "work_contribution:skipped": "Contribution de travail ignorée",
  "observation:imported": "Observation importée",
  "observation:rejected": "Observation refusée",
  "observation:skipped": "Observation ignorée",
  "finding:imported": "Finding importé",
  "finding:rejected": "Finding refusé",
  "finding:skipped": "Finding ignoré",
};

function splitTransition(
  transition: string | undefined,
): [string, string] | null {
  if (transition === undefined || !transition.includes("->")) return null;
  const [from, to] = transition.split("->");
  return from === undefined || to === undefined ? null : [from, to];
}

function statusLabel(value: string): string | null {
  if (value === "-") return null;
  return TASK_STATUS_LABELS[value as TaskStatus] ?? null;
}

/**
 * One French sentence (or the raw token when it cannot be translated) for a
 * timeline item. Never invents a value: an unknown union member falls back to
 * the summary the server sent.
 */
export function timelineSummaryLabel(item: TimelineItemV1): string {
  const summary = item.summary;
  const parts = summary.split(":");

  switch (item.item_type) {
    case "task_event": {
      const [eventType, transition] = parts;
      const eventLabel = TASK_EVENT_LABELS[eventType as TaskEventType];
      if (eventLabel === undefined) return summary;
      const statuses = splitTransition(transition);
      if (statuses === null) return eventLabel;
      const from = statusLabel(statuses[0]);
      const to = statusLabel(statuses[1]);
      return from === null || to === null
        ? eventLabel
        : `${eventLabel} · ${from} → ${to}`;
    }
    case "decision": {
      const [decisionType, targetType] = parts;
      const typeLabel = DECISION_TYPE_LABELS[decisionType ?? ""];
      if (typeLabel === undefined) return summary;
      if (targetType === undefined) return typeLabel;
      return `${typeLabel} · cible : ${
        DECISION_TARGET_LABELS[targetType] ?? targetType
      }`;
    }
    case "observation": {
      const [kind, objectType] = parts;
      const kindLabel =
        EVIDENCE_KIND_LABELS[kind as keyof typeof EVIDENCE_KIND_LABELS];
      if (kindLabel === undefined || objectType === undefined) return summary;
      return `${kindLabel} · objet : ${
        OBJECT_TYPE_LABELS[objectType] ?? objectType
      }`;
    }
    case "bridge_consultation":
    case "tool_invocation": {
      const [toolName, status] = parts;
      const toolLabel = TOOL_LABELS[toolName ?? ""];
      const statusLabelText =
        INVOCATION_STATUS_LABELS[status as ToolInvocationStatus];
      if (toolLabel === undefined || statusLabelText === undefined) {
        return summary;
      }
      return `${toolLabel} · ${statusLabelText}`;
    }
    case "plan_version": {
      const [prefix, version, parseStatus] = parts;
      if (prefix !== "plan_version" || version === undefined) return summary;
      return `Version de plan ${version} · ${
        PLAN_PARSE_LABELS[parseStatus ?? ""] ?? parseStatus ?? "statut inconnu"
      }`;
    }
    case "handoff": {
      const [prefix, version, transition] = parts;
      const statuses = splitTransition(transition);
      if (prefix !== "handoff" || version === undefined || statuses === null) {
        return summary;
      }
      const from =
        TASK_ACTOR_LABELS[statuses[0] as keyof typeof TASK_ACTOR_LABELS];
      const to =
        TASK_ACTOR_LABELS[statuses[1] as keyof typeof TASK_ACTOR_LABELS];
      if (from === undefined || to === undefined) return summary;
      return `Passation ${version} · ${from} → ${to}`;
    }
    case "contribution": {
      const [prefix, category, importStatus] = parts;
      if (prefix !== "contribution") return summary;
      const label =
        CONTRIBUTION_LABELS[`${category ?? ""}:${importStatus ?? ""}`];
      if (label !== undefined) return label;
      const categoryLabel =
        category === "work_contribution"
          ? "Contribution de travail"
          : category === "observation"
            ? "Observation"
            : category === "finding"
              ? "Finding"
              : category;
      const statusText: Record<WorkContributionImportStatus, string> = {
        imported: "importé",
        rejected: "refusé",
        skipped: "ignoré",
      };
      return `${categoryLabel ?? summary} · ${
        statusText[importStatus as WorkContributionImportStatus] ??
        importStatus ??
        "statut inconnu"
      }`;
    }
    case "agent_session": {
      const [prefix, adapterKind, state] = parts;
      if (prefix !== "agent_session" || adapterKind === undefined) {
        return summary;
      }
      return `Session ${
        ADAPTER_LABELS[adapterKind as OperatorAdapterKind] ?? adapterKind
      } · ${
        SESSION_STATE_LABELS[state as AgentSessionState] ??
        state ??
        "état inconnu"
      }`;
    }
    case "instruction": {
      const [prefix, state] = parts;
      if (prefix !== "instruction" || state === undefined) return summary;
      return `Instruction humaine · ${
        INSTRUCTION_JOURNAL_STATE_LABELS[state as InstructionState] ?? state
      }`;
    }
  }
  return summary;
}
