import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import {
  decideSourceRelationship,
  getSubjectWorkbench,
  launchSubjectCollection,
  retryCollectedSource,
  reviewClaim,
  reviewIndicator,
  type CollectedSource,
  type EvidenceClaim,
  type EvidenceIndicator,
  type SourceRole,
} from "../api/collection";
import { JobStatusCard } from "./JobStatusCard";

type Tab = "sources" | "evidence" | "ioc" | "extraction";

export function SubjectWorkbench({ subjectId }: { subjectId: string }) {
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<Tab>("sources");
  const [jobId, setJobId] = useState<string | null>(null);
  const workbench = useQuery({
    queryKey: ["subject-workbench", subjectId],
    queryFn: () => getSubjectWorkbench(subjectId),
    refetchInterval: jobId ? 2_000 : false,
  });
  const launch = useMutation({
    mutationFn: () => launchSubjectCollection(subjectId),
    onSuccess: (result) => {
      setJobId(result.job_id);
      void queryClient.invalidateQueries({
        queryKey: ["subject-workbench", subjectId],
      });
    },
  });

  return (
    <section className="subject-workbench">
      <a href="/editions">← Retour aux éditions</a>
      <div className="detail-heading">
        <div>
          <p className="eyebrow">Production evidence-first</p>
          <h1>Workbench Sujet</h1>
        </div>
        <button
          className="button"
          disabled={launch.isPending}
          onClick={() => launch.mutate()}
        >
          {launch.isPending ? "Démarrage…" : "Démarrer la collecte"}
        </button>
      </div>
      <p className="verification-warning" role="note">
        La collecte est une action explicite. Le contenu distant est traité
        comme une donnée non fiable et aucun JavaScript n’est exécuté.
      </p>
      {launch.error ? (
        <p role="alert" className="error-message">
          {launch.error.message}
        </p>
      ) : null}
      {jobId ? <JobStatusCard jobId={jobId} /> : null}
      <nav className="workbench-tabs" aria-label="Onglets du sujet">
        {(
          [
            ["sources", "Sources"],
            ["evidence", "Preuves"],
            ["ioc", "IOC"],
            ["extraction", "Extraction"],
          ] as const
        ).map(([value, label]) => (
          <button
            key={value}
            aria-pressed={tab === value}
            onClick={() => setTab(value)}
          >
            {label}
          </button>
        ))}
      </nav>
      {workbench.isPending ? <p role="status">Chargement du sujet…</p> : null}
      {workbench.isError ? (
        <p role="alert" className="error-message">
          Le workbench est inaccessible.
        </p>
      ) : null}
      {workbench.data && tab === "sources" ? (
        <SourcesTab subjectId={subjectId} sources={workbench.data.sources} />
      ) : null}
      {workbench.data && tab === "evidence" ? (
        <EvidenceTab subjectId={subjectId} claims={workbench.data.claims} />
      ) : null}
      {workbench.data && tab === "ioc" ? (
        <IndicatorsTab
          subjectId={subjectId}
          indicators={workbench.data.indicators}
        />
      ) : null}
      {workbench.data && tab === "extraction" ? (
        <ExtractionTab claims={workbench.data.claims} />
      ) : null}
    </section>
  );
}

function SourcesTab({
  subjectId,
  sources,
}: {
  subjectId: string;
  sources: CollectedSource[];
}) {
  const queryClient = useQueryClient();
  const [roles, setRoles] = useState<Record<string, SourceRole>>({});
  const action = useMutation({
    mutationFn: (operation: () => Promise<unknown>) => operation(),
    onSuccess: () =>
      queryClient.invalidateQueries({
        queryKey: ["subject-workbench", subjectId],
      }),
  });
  if (!sources.length)
    return (
      <p className="empty-state">Aucune collecte n’a encore été demandée.</p>
    );
  return (
    <div className="workbench-list">
      {sources.map((source) => {
        const attempt = source.latest_attempt;
        const role = roles[source.id] ?? source.proposed_role;
        return (
          <article key={source.id} className="evidence-card">
            <h2>{source.requested_url}</h2>
            <p>
              État <strong>{source.state}</strong> · disponibilité{" "}
              {attempt?.outcome ?? "inconnue"}
            </p>
            <dl className="edition-facts">
              <div>
                <dt>URL finale</dt>
                <dd>{attempt?.final_url ?? "—"}</dd>
              </div>
              <div>
                <dt>SHA-256 brut encodé</dt>
                <dd className="technical-value">
                  {attempt?.encoded_sha256 ?? "—"}
                </dd>
              </div>
              <div>
                <dt>Taille brute encodée</dt>
                <dd>{attempt?.encoded_size ?? "—"} octets</dd>
              </div>
              <div>
                <dt>SHA-256 contenu décodé</dt>
                <dd className="technical-value">
                  {attempt?.decoded_sha256 ?? "—"}
                </dd>
              </div>
              <div>
                <dt>Taille contenu décodé</dt>
                <dd>{attempt?.decoded_size ?? "—"} octets</dd>
              </div>
              <div>
                <dt>Content-Encoding</dt>
                <dd>{attempt?.content_encoding ?? "identity"}</dd>
              </div>
              <div>
                <dt>Acquisition</dt>
                <dd>{attempt?.completed_at ?? "—"}</dd>
              </div>
              <div>
                <dt>Type détecté</dt>
                <dd>{attempt?.detected_content_type ?? "—"}</dd>
              </div>
            </dl>
            <p>
              Relation proposée : <strong>{source.proposed_role}</strong> ·
              preuve <strong>{source.relationship_status}</strong>
            </p>
            {source.error_reason ? (
              <p className="error-message">{source.error_reason}</p>
            ) : null}
            <div className="editorial-actions">
              <button
                className="button button--secondary"
                disabled={
                  action.isPending ||
                  [
                    "completed",
                    "blocked",
                    "fetching",
                    "failed_terminal",
                  ].includes(source.state)
                }
                onClick={() =>
                  action.mutate(() =>
                    retryCollectedSource(subjectId, source.id),
                  )
                }
              >
                {source.state === "completed"
                  ? "Déjà terminée"
                  : source.state === "blocked"
                    ? "Bloquée par sécurité"
                    : source.state === "failed_terminal"
                      ? "Nouvelle politique requise"
                      : source.state === "fetching"
                        ? "Téléchargement en cours"
                        : "Relancer cette source"}
              </button>
              <label>
                Relation
                <select
                  value={role}
                  onChange={(event) =>
                    setRoles((current) => ({
                      ...current,
                      [source.id]: event.target.value as SourceRole,
                    }))
                  }
                >
                  {[
                    "primary",
                    "independent",
                    "relay",
                    "aggregator",
                    "social",
                    "unknown",
                  ].map((value) => (
                    <option key={value}>{value}</option>
                  ))}
                </select>
              </label>
              <button
                className="button"
                disabled={action.isPending}
                onClick={() =>
                  action.mutate(() =>
                    decideSourceRelationship(subjectId, source.id, role),
                  )
                }
              >
                {role === source.proposed_role
                  ? "Valider la relation"
                  : "Corriger la relation"}
              </button>
            </div>
          </article>
        );
      })}
    </div>
  );
}

function EvidenceTab({
  subjectId,
  claims,
}: {
  subjectId: string;
  claims: EvidenceClaim[];
}) {
  if (!claims.length)
    return <p className="empty-state">Aucune preuve structurée disponible.</p>;
  return (
    <div className="workbench-list">
      {claims.map((claim) => (
        <EvidenceReviewCard
          key={claim.id}
          subjectId={subjectId}
          claim={claim}
        />
      ))}
    </div>
  );
}

function EvidenceReviewCard({
  subjectId,
  claim,
}: {
  subjectId: string;
  claim: EvidenceClaim;
}) {
  const queryClient = useQueryClient();
  const action = useMutation({
    mutationFn: ({
      operation,
      corrected,
    }: {
      operation: "validate" | "correct" | "reject";
      corrected?: string;
    }) => reviewClaim(subjectId, claim.id, operation, corrected),
    onSuccess: () =>
      queryClient.invalidateQueries({
        queryKey: ["subject-workbench", subjectId],
      }),
  });
  return (
    <article className="evidence-card">
      <h2>{claim.current_value}</h2>
      <p>
        {claim.kind} · {claim.status} · source {claim.source_id}
      </p>
      <blockquote>
        <mark>{claim.passage}</mark>
      </blockquote>
      <ReviewActions pending={action.isPending} onReview={action.mutate} />
    </article>
  );
}

function IndicatorsTab({
  subjectId,
  indicators,
}: {
  subjectId: string;
  indicators: EvidenceIndicator[];
}) {
  const queryClient = useQueryClient();
  const action = useMutation({
    mutationFn: ({
      indicator,
      operation,
      corrected,
    }: {
      indicator: EvidenceIndicator;
      operation: "validate" | "correct" | "reject";
      corrected?: string;
    }) => reviewIndicator(subjectId, indicator.id, operation, corrected),
    onSuccess: () =>
      queryClient.invalidateQueries({
        queryKey: ["subject-workbench", subjectId],
      }),
  });
  if (!indicators.length)
    return <p className="empty-state">Aucun IOC déterministe extrait.</p>;
  return (
    <div className="workbench-list">
      {indicators.map((indicator) => (
        <article className="evidence-card" key={indicator.id}>
          <h2 className="technical-value">{indicator.current_value}</h2>
          <p>
            Original : {indicator.original_value} · normalisé :{" "}
            {indicator.normalized_value}
          </p>
          <p>
            {indicator.kind} · source {indicator.source_id} · {indicator.status}
          </p>
          <ReviewActions
            pending={action.isPending}
            onReview={({ operation, corrected }) =>
              action.mutate({ indicator, operation, corrected })
            }
          />
        </article>
      ))}
    </div>
  );
}

function ReviewActions({
  pending,
  onReview,
}: {
  pending: boolean;
  onReview: (value: {
    operation: "validate" | "correct" | "reject";
    corrected?: string;
  }) => void;
}) {
  const [correction, setCorrection] = useState("");
  return (
    <div className="editorial-actions">
      <button
        disabled={pending}
        onClick={() => onReview({ operation: "validate" })}
      >
        Valider
      </button>
      <label>
        Correction
        <input
          value={correction}
          onChange={(event) => setCorrection(event.target.value)}
        />
      </label>
      <button
        disabled={pending || !correction.trim()}
        onClick={() =>
          onReview({ operation: "correct", corrected: correction })
        }
      >
        Corriger
      </button>
      <button
        disabled={pending}
        onClick={() => onReview({ operation: "reject" })}
      >
        Rejeter
      </button>
    </div>
  );
}

function ExtractionTab({ claims }: { claims: EvidenceClaim[] }) {
  const structured = claims.filter((claim) =>
    [
      "actors",
      "campaigns",
      "malware",
      "tools",
      "infection_chain",
      "ttp",
      "victimology",
    ].includes(String(claim.extraction_payload.category)),
  );
  return structured.length ? (
    <dl className="edition-facts">
      {structured.map((claim) => (
        <div key={claim.id}>
          <dt>{String(claim.extraction_payload.category)}</dt>
          <dd>{claim.current_value}</dd>
        </div>
      ))}
    </dl>
  ) : (
    <p className="empty-state">
      Aucune chaîne d’infection, outil, TTP ou victimologie extraite.
    </p>
  );
}
