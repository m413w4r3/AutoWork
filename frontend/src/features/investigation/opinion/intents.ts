import type {
  AnalystObservationRelation,
  AnalystObservationState,
  NoteProposalConsultationType,
  ProseOpinionBlockType,
} from "../api/opinionProposals";
import {
  BLOCK_LABELS,
  NOTE_RELATION_LABELS,
  NOTE_STATE_LABELS,
} from "./labels";

export type DraftingIntent =
  | "rediger"
  | "developper"
  | "verifier"
  | "corriger"
  | "reformuler"
  | "ajouter_limite"
  | "raccourcir"
  | "reorganiser"
  | "mettre_a_jour";

export interface DraftingIntentOption {
  id: DraftingIntent;
  label: string;
  consultationType: NoteProposalConsultationType;
  /** A review unit must target an existing Bridge-drafted note. */
  requiresCurrentBridgeNote: boolean;
  instruction: string;
}

export const DRAFTING_INTENTS: DraftingIntentOption[] = [
  {
    id: "rediger",
    label: "Rédiger",
    consultationType: "draft_text",
    requiresCurrentBridgeNote: false,
    instruction:
      "Rédige le texte exact d’une note de l’analyste à cette ancre.",
  },
  {
    id: "developper",
    label: "Développer",
    consultationType: "draft_text",
    requiresCurrentBridgeNote: false,
    instruction:
      "Développe la note à cette ancre en conservant strictement les faits cités.",
  },
  {
    id: "verifier",
    label: "Vérifier",
    consultationType: "review_text",
    requiresCurrentBridgeNote: true,
    instruction:
      "Vérifie la note de l’analyste existante à cette ancre. Signale les affirmations non soutenues, les confusions entre observation et inférence, et les limites manquantes. Ne réécris pas le texte.",
  },
  {
    id: "corriger",
    label: "Corriger",
    consultationType: "draft_text",
    requiresCurrentBridgeNote: false,
    instruction:
      "Corrige la note à cette ancre sans ajouter aucun fait non cité.",
  },
  {
    id: "reformuler",
    label: "Reformuler",
    consultationType: "draft_text",
    requiresCurrentBridgeNote: false,
    instruction:
      "Reformule la note à cette ancre sans changer les faits, les qualifications ni les citations.",
  },
  {
    id: "ajouter_limite",
    label: "Ajouter une limite",
    consultationType: "draft_text",
    requiresCurrentBridgeNote: false,
    instruction:
      "Réécris la note à cette ancre en ajoutant explicitement une limite fondée sur les limites du dossier.",
  },
  {
    id: "raccourcir",
    label: "Raccourcir",
    consultationType: "draft_text",
    requiresCurrentBridgeNote: false,
    instruction:
      "Raccourcis la note à cette ancre sans perdre les faits, les qualifications ni les citations.",
  },
  {
    id: "reorganiser",
    label: "Réorganiser",
    consultationType: "draft_text",
    requiresCurrentBridgeNote: false,
    instruction:
      "Réorganise la note à cette ancre sans changer les faits ni les citations.",
  },
  {
    id: "mettre_a_jour",
    label: "Mettre à jour",
    consultationType: "draft_text",
    requiresCurrentBridgeNote: false,
    instruction:
      "Mets à jour la note à cette ancre avec les références citées ci-dessous.",
  },
];

export function getDraftingIntent(id: DraftingIntent): DraftingIntentOption {
  const option = DRAFTING_INTENTS.find((item) => item.id === id);
  if (option === undefined) {
    throw new Error(`Unknown drafting intent: ${id}`);
  }
  return option;
}

export interface DraftingQuestionContext {
  anchor: string;
  blockType: ProseOpinionBlockType;
  state: AnalystObservationState;
  relation: AnalystObservationRelation;
  findingRefs: string[];
  observationRefs: string[];
}

/**
 * The request body never carries note prose: only this bounded instruction is
 * sent to Bridge through the canonical `advisor.consult` path.
 */
export function buildDraftingQuestion(
  intent: DraftingIntent,
  context: DraftingQuestionContext,
): string {
  const option = getDraftingIntent(intent);
  const lines = [
    `${option.instruction} Ancre : ${context.anchor}. Bloc : ${
      BLOCK_LABELS[context.blockType]
    } (forme analyst_note).`,
    `État déclaré de la note : ${
      NOTE_STATE_LABELS[context.state]
    } ; relation : ${NOTE_RELATION_LABELS[context.relation]}.`,
    "Fonde chaque affirmation sur les références citées ci-dessous et n’invente aucune mesure, aucun octet et aucune relation absente du dossier.",
  ];
  if (context.findingRefs.length > 0) {
    lines.push(`Findings cités : ${context.findingRefs.join(", ")}.`);
  }
  if (context.observationRefs.length > 0) {
    lines.push(`Observations citées : ${context.observationRefs.join(", ")}.`);
  }
  lines.push(
    option.consultationType === "review_text"
      ? "Retourne uniquement les remarques de vérification, sans préambule."
      : "Retourne uniquement le texte de la note, sans préambule ni commentaire.",
  );
  return lines.join("\n");
}
