/**
 * Human G3 qualification of one investigation sample association.
 *
 * The form reuses the existing governance route (`useQualifyInvestigationSample`)
 * with its CAS `expected_version`, a mandatory reason, and at least one
 * observation of the same investigation when confirming or rejecting. The
 * canonical qualification history is read from `GET …/samples`; nothing here
 * promotes a candidate automatically.
 */

import { useId, useState, type FormEvent } from "react";
import { useQueryClient } from "@tanstack/react-query";

import {
  useInvestigation,
  type InvestigationObservation,
} from "../../../api/investigation";
import {
  useInvestigationSamples,
  useQualifyInvestigationSample,
  type InvestigationSampleDetail,
  type SampleQualificationState,
} from "../../../api/investigationGovernance";
import { bootstrapKeys } from "../api";
import { EVIDENCE_KIND_LABELS } from "../opinion/labels";
import {
  SAMPLE_ORIGIN_LABELS,
  SAMPLE_QUALIFICATION_LABELS,
  formatTimestamp,
  labelOf,
  workshopErrorText,
} from "./samplesLabels";

const QUALIFICATION_OPTIONS: readonly SampleQualificationState[] = [
  "candidate",
  "related",
  "uncertain",
  "confirmed",
  "rejected",
];

function sampleTitle(sample: InvestigationSampleDetail): string {
  return `${sample.sample_sha256.slice(0, 12)}… · ${sample.id.slice(0, 8)}`;
}

function QualificationForm({
  investigationId,
  sample,
  observations,
}: {
  investigationId: string;
  sample: InvestigationSampleDetail;
  observations: readonly InvestigationObservation[];
}) {
  const baseId = useId();
  const queryClient = useQueryClient();
  const qualify = useQualifyInvestigationSample();
  const [qualification, setQualification] = useState<SampleQualificationState>(
    sample.qualification,
  );
  const [reason, setReason] = useState("");
  const [evidenceRefs, setEvidenceRefs] = useState<string[]>([]);
  const [confirmed, setConfirmed] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const requiresEvidence =
    qualification === "confirmed" || qualification === "rejected";
  const canSubmit =
    reason.trim() !== "" &&
    reason.trim().length <= 4_000 &&
    confirmed &&
    qualification !== sample.qualification &&
    (!requiresEvidence || evidenceRefs.length > 0) &&
    !qualify.isPending;

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSubmit) return;
    setMessage(null);
    try {
      const result = await qualify.mutateAsync({
        investigation_id: investigationId,
        association_id: sample.id,
        expected_version: sample.version,
        qualification,
        reason: reason.trim(),
        evidence_refs: evidenceRefs,
        correlation_id:
          globalThis.crypto?.randomUUID?.() ??
          `ui-${Date.now()}-${Math.random().toString(16).slice(2)}`,
      });
      setMessage(
        `Décision G3 enregistrée : ${SAMPLE_QUALIFICATION_LABELS[sample.qualification]} → ${SAMPLE_QUALIFICATION_LABELS[result.association.qualification]} · version ${result.association.version}.`,
      );
      setReason("");
      setEvidenceRefs([]);
      setConfirmed(false);
      // The acquisition table reads the bootstrap inventory, not the
      // governance sample list: refresh that projection explicitly.
      void queryClient.invalidateQueries({
        queryKey: bootstrapKeys.all(investigationId),
      });
    } catch {
      return;
    }
  }

  return (
    <form
      className="workspace-notes__form"
      onSubmit={(event) => void submit(event)}
    >
      <label htmlFor={`${baseId}-qualification`}>
        Décision G3 pour {sampleTitle(sample)}
      </label>
      <select
        id={`${baseId}-qualification`}
        value={qualification}
        onChange={(event) => {
          const value = event.currentTarget.value;
          if (QUALIFICATION_OPTIONS.some((candidate) => candidate === value)) {
            setQualification(value as SampleQualificationState);
          }
        }}
      >
        {QUALIFICATION_OPTIONS.map((value) => (
          <option key={value} value={value}>
            {SAMPLE_QUALIFICATION_LABELS[value]}
          </option>
        ))}
      </select>
      <label htmlFor={`${baseId}-reason`}>
        Motif de la décision (obligatoire)
      </label>
      <textarea
        id={`${baseId}-reason`}
        required
        maxLength={4_000}
        value={reason}
        onChange={(event) => setReason(event.currentTarget.value)}
      />
      <label htmlFor={`${baseId}-evidence`}>
        Observations de la même enquête
        {requiresEvidence
          ? " (au moins une obligatoire pour confirmer ou rejeter)"
          : " (facultatives)"}
      </label>
      <select
        id={`${baseId}-evidence`}
        multiple
        value={evidenceRefs}
        onChange={(event) =>
          setEvidenceRefs(
            Array.from(
              event.currentTarget.selectedOptions,
              (option) => option.value,
            ),
          )
        }
      >
        {observations.map((observation) => (
          <option key={observation.id} value={observation.id}>
            {labelOf(EVIDENCE_KIND_LABELS, observation.kind)} ·{" "}
            {observation.source} · {formatTimestamp(observation.observed_at)} ·{" "}
            {observation.sha256.slice(0, 12)}…
          </option>
        ))}
      </select>
      {observations.length === 0 ? (
        <p className="samples-results__meta">
          Aucune observation de cette enquête n’est disponible : seules les
          décisions sans preuve exigée (candidat, lié, incertain) restent
          possibles.
        </p>
      ) : null}
      <label className="workspace-notes__meta">
        <input
          type="checkbox"
          checked={confirmed}
          onChange={(event) => setConfirmed(event.currentTarget.checked)}
        />{" "}
        Je confirme cette décision G3 ; l’acteur sera l’identité authentifiée
        côté serveur.
      </label>
      <div className="workspace-notes__actions">
        <button className="button" type="submit" disabled={!canSubmit}>
          {qualify.isPending
            ? "Enregistrement de la décision…"
            : "Enregistrer la décision G3"}
        </button>
      </div>
      <p className="samples-results__meta">
        Version CAS envoyée : <code>{sample.version}</code> · acquisition ≠
        confirmation · aucune automatisation ne promeut un candidat.
      </p>
      {qualify.error !== null ? (
        <p className="error-message" role="alert">
          {workshopErrorText(qualify.error, "La décision G3 a été refusée.")}
        </p>
      ) : null}
      {message === null ? null : (
        <p className="samples-results__note" role="status">
          {message}
        </p>
      )}
    </form>
  );
}

export function SampleQualificationPanel({
  investigationId,
}: {
  investigationId: string;
}) {
  const samplesQuery = useInvestigationSamples(investigationId);
  const investigationQuery = useInvestigation(investigationId);
  const [selectedId, setSelectedId] = useState("");
  const samples = samplesQuery.data ?? [];
  const selected =
    samples.find((sample) => sample.id === selectedId) ?? samples[0] ?? null;
  const observations = investigationQuery.data?.observations ?? [];

  return (
    <section
      className="samples-results__section"
      aria-label="Qualification G3 des échantillons"
    >
      <h2>Qualification G3 des échantillons</h2>
      <p className="samples-results__meta">
        Une acquisition reste candidate jusqu’à une décision humaine motivée,
        portant la version CAS de l’association et au moins une observation de
        cette enquête pour confirmer ou rejeter.
      </p>
      {samplesQuery.isPending ? (
        <p role="status">Chargement des associations et de leur historique…</p>
      ) : samplesQuery.error !== null ? (
        <p className="error-message" role="alert">
          {workshopErrorText(
            samplesQuery.error,
            "Les associations d’échantillons sont indisponibles.",
          )}
        </p>
      ) : samples.length === 0 ? (
        <p>Aucun échantillon associé à cette enquête.</p>
      ) : (
        <>
          <label htmlFor="samples-g3-association">
            Association d’échantillon
          </label>
          <select
            id="samples-g3-association"
            value={selected?.id ?? ""}
            onChange={(event) => setSelectedId(event.currentTarget.value)}
          >
            {samples.map((sample) => (
              <option key={sample.id} value={sample.id}>
                {sampleTitle(sample)} ·{" "}
                {SAMPLE_QUALIFICATION_LABELS[sample.qualification]} ·{" "}
                {SAMPLE_ORIGIN_LABELS[sample.origin]}
              </option>
            ))}
          </select>
          {selected === null ? null : (
            <>
              <p className="samples-results__meta">
                SHA-256 <code>{selected.sample_sha256}</code> ·{" "}
                {SAMPLE_ORIGIN_LABELS[selected.origin]} · version CAS{" "}
                {selected.version}
                {selected.origin === "seed"
                  ? " · échantillon déjà présent dans l’article étudié"
                  : ""}
                .
              </p>
              <h3>Historique des qualifications</h3>
              {selected.changes.length === 0 ? (
                <p>Aucun changement de qualification enregistré.</p>
              ) : (
                <ol className="samples-results__list">
                  {selected.changes.map((change, index) => (
                    <li key={`${change.changed_at}-${index}`}>
                      {SAMPLE_QUALIFICATION_LABELS[change.from_qualification]} →{" "}
                      {SAMPLE_QUALIFICATION_LABELS[change.to_qualification]} ·{" "}
                      {change.actor_kind} {change.actor_id} ·{" "}
                      {formatTimestamp(change.changed_at)} · {change.reason}
                    </li>
                  ))}
                </ol>
              )}
              {investigationQuery.error !== null ? (
                <p className="error-message" role="alert">
                  {workshopErrorText(
                    investigationQuery.error,
                    "Les observations de l’enquête sont indisponibles : les décisions exigeant une preuve restent bloquées.",
                  )}
                </p>
              ) : null}
              <QualificationForm
                key={selected.id}
                investigationId={investigationId}
                sample={selected}
                observations={observations}
              />
            </>
          )}
        </>
      )}
    </section>
  );
}
