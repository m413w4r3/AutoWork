import { analystOpinionPreviewPdfUrl } from "../../../api/analystOpinion";
import type { CitationViolationPayload } from "../../../api/investigation";
import { classifyAnchor } from "./anchor";
import type { AnalystNoteV1, OpinionVersionV1 } from "./types";

export type PreApprovalStatus = "ok" | "warning" | "blocked";

export interface PreApprovalCheck {
  key: string;
  label: string;
  status: PreApprovalStatus;
  detail: string;
}

export interface PreApprovalReport {
  checks: PreApprovalCheck[];
  blocked: boolean;
  bridgeGapRequiresAcknowledgement: boolean;
  previewUrl: string | null;
}

function plural(count: number, singular: string, pluralForm: string): string {
  return `${count} ${count === 1 ? singular : pluralForm}`;
}

/**
 * Client-side projection of the server checks. It never replaces them: it
 * exposes citations, qualifications, unsupported passages, observation vs
 * inference, Bridge drafting gaps, anchors and the real PDF preview before a
 * human approval.
 */
export function buildPreApprovalReport(
  opinion: OpinionVersionV1,
  citationViolations: CitationViolationPayload[],
  notes: AnalystNoteV1[],
): PreApprovalReport {
  const checks: PreApprovalCheck[] = [];
  const review = opinion.editorial_review;
  const blocks = opinion.blocks;

  checks.push(
    citationViolations.length === 0
      ? {
          key: "citations",
          label: "Citations",
          status: "ok",
          detail: "Aucune violation de citation renvoyée par le serveur.",
        }
      : {
          key: "citations",
          label: "Citations",
          status: "blocked",
          detail: `${plural(
            citationViolations.length,
            "violation",
            "violations",
          )} : ${citationViolations.map((item) => item.code).join(", ")}`,
        },
  );

  const unsupportedFacts = blocks.filter(
    (block) =>
      block.qualification === "fact" &&
      block.finding_refs.length === 0 &&
      block.evidence_refs.length === 0,
  );
  const qualificationCounts = blocks.reduce<Record<string, number>>(
    (counts, block) => {
      counts[block.qualification] = (counts[block.qualification] ?? 0) + 1;
      return counts;
    },
    {},
  );
  checks.push({
    key: "qualifications",
    label: "Qualifications et passages non supportés",
    status: unsupportedFacts.length === 0 ? "ok" : "warning",
    detail:
      unsupportedFacts.length === 0
        ? `Faits : ${qualificationCounts.fact ?? 0} · inférences : ${
            qualificationCounts.inference ?? 0
          } · hypothèses : ${qualificationCounts.hypothesis ?? 0}.`
        : `${plural(
            unsupportedFacts.length,
            "passage qualifié de fait sans référence",
            "passages qualifiés de fait sans référence",
          )}.`,
  });

  const observedWithoutEvidence = notes.filter(
    (note) => note.state === "observed" && note.evidence_refs.length === 0,
  );
  checks.push({
    key: "observation_inference",
    label: "Observation contre inférence",
    status: observedWithoutEvidence.length === 0 ? "ok" : "warning",
    detail:
      observedWithoutEvidence.length === 0
        ? `Notes : ${plural(notes.length, "note", "notes")} (observées ${
            notes.filter((note) => note.state === "observed").length
          }, inférées ${
            notes.filter((note) => note.state === "inferred").length
          }, non résolues ${
            notes.filter((note) => note.state === "unresolved").length
          }).`
        : `${plural(
            observedWithoutEvidence.length,
            "note observée sans observation de preuve",
            "notes observées sans observation de preuve",
          )}.`,
  });

  const bridgeDrafting = review?.bridge_drafting;
  checks.push(
    bridgeDrafting === undefined
      ? {
          key: "bridge_drafting",
          label: "Lacunes de rédaction Bridge",
          status: "warning",
          detail: "Aucune revue éditoriale Bridge n’est disponible.",
        }
      : {
          key: "bridge_drafting",
          label: "Lacunes de rédaction Bridge",
          status: bridgeDrafting.status === "complete" ? "ok" : "warning",
          detail: `${bridgeDrafting.bridge_authored_section_count}/${
            bridgeDrafting.section_count
          } sections rédigées par Bridge · ${plural(
            bridgeDrafting.unlinked_sections.length,
            "section sans rédaction liée",
            "sections sans rédaction liée",
          )}${
            bridgeDrafting.diagnosis === null
              ? ""
              : ` · ${bridgeDrafting.diagnosis}`
          }.`,
        },
  );

  const invalidAnchors = notes.filter(
    (note) => classifyAnchor(note.anchor) === null,
  );
  const anchorCounts = new Map<string, number>();
  for (const note of notes) {
    anchorCounts.set(note.anchor, (anchorCounts.get(note.anchor) ?? 0) + 1);
  }
  const duplicateAnchors = [...anchorCounts.entries()].filter(
    ([, count]) => count > 1,
  );
  checks.push({
    key: "anchors",
    label: "Ancres",
    status:
      invalidAnchors.length === 0 && duplicateAnchors.length === 0
        ? "ok"
        : "blocked",
    detail:
      invalidAnchors.length > 0
        ? `${plural(
            invalidAnchors.length,
            "ancre invalide",
            "ancres invalides",
          )} : ${invalidAnchors.map((note) => note.anchor).join(", ")}`
        : duplicateAnchors.length > 0
          ? `${plural(
              duplicateAnchors.length,
              "ancre porte plusieurs notes",
              "ancres portent plusieurs notes",
            )} : ${duplicateAnchors
              .map(([anchor]) => anchor)
              .join(", ")}. Une note par ancre est attendue.`
          : `${plural(notes.length, "ancre", "ancres")} conforme(s) (lead ou section).`,
  });

  const markers = review?.publication_text_markers ?? [];
  checks.push({
    key: "text_markers",
    label: "Marqueurs internes du texte publiable",
    status: markers.length === 0 ? "ok" : "blocked",
    detail:
      markers.length === 0
        ? "Aucun marqueur interne détecté."
        : markers.join(", "),
  });

  const missingAnnexes = review?.missing_annex_references ?? [];
  checks.push({
    key: "annexes",
    label: "Annexes citées",
    status: missingAnnexes.length === 0 ? "ok" : "warning",
    detail:
      missingAnnexes.length === 0
        ? "Toutes les annexes citées sont présentes."
        : `Annexes absentes : ${missingAnnexes.join(", ")}.`,
  });

  const reviewWarnings = review?.warnings ?? [];
  checks.push({
    key: "review_warnings",
    label: "Avertissements de revue éditoriale",
    status: reviewWarnings.length === 0 ? "ok" : "warning",
    detail:
      reviewWarnings.length === 0
        ? "Aucun avertissement supplémentaire."
        : reviewWarnings.join(" · "),
  });

  const previewUrl = analystOpinionPreviewPdfUrl(
    opinion,
    opinion.provenance.base_publication_sha256,
  );

  return {
    checks,
    blocked: checks.some((check) => check.status === "blocked"),
    bridgeGapRequiresAcknowledgement:
      bridgeDrafting?.requires_acknowledgement === true,
    previewUrl,
  };
}
