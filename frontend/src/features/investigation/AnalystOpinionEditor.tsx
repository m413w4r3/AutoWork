import { useEffect, useMemo, useState } from "react";

import {
  useAnalystOpinion,
  useApproveAnalystOpinion,
  useComposeAnalystOpinion,
  usePublishAnalystOpinion,
  useRequestAnalystOpinionChanges,
  useReviseAnalystOpinion,
  type PublishOpinionResponse,
  type OpinionBlockEdit,
} from "../../api/analystOpinion";
import {
  InvestigationApiError,
  useProtocolModels,
  type CitationViolationPayload,
  type InvestigationOpinionDraft,
  type OpinionBlockType,
  type OpinionQualification,
  type ProtocolDiagramKind,
} from "../../api/investigation";

const BLOCK_ORDER: OpinionBlockType[] = [
  "key_findings",
  "additional_samples",
  "infection_chain_and_capabilities",
  "network_and_infrastructure",
  "claim_verification",
  "proposed_detections",
  "uncertainties_and_next_leads",
];

const BLOCK_LABELS: Record<OpinionBlockType | "implant_materials", string> = {
  key_findings: "Apports de l’enquête",
  additional_samples: "Échantillons complémentaires",
  infection_chain_and_capabilities: "Chaîne d’infection et capacités",
  network_and_infrastructure: "Activité réseau et infrastructure",
  claim_verification: "Vérification des affirmations",
  proposed_detections: "Détections proposées",
  uncertainties_and_next_leads: "Incertitudes et prochaines pistes",
  implant_materials: "Matériel relatif à l’implant",
};

const QUALIFICATION_LABELS: Record<OpinionQualification, string> = {
  fact: "Fait",
  inference: "Inférence",
  hypothesis: "Hypothèse",
};

const PROTOCOL_STATUS_LABELS: Record<string, string> = {
  observed: "Observé",
  inferred: "Inféré",
  unresolved: "Non résolu",
  contradicted: "Contredit",
};

export function AnalystOpinionEditor({
  investigationId,
  currentDraft,
  compositionHref,
}: {
  investigationId: string;
  currentDraft: InvestigationOpinionDraft | null;
  compositionHref: string;
}) {
  const opinionQuery = useAnalystOpinion(investigationId);
  const protocolModelsQuery = useProtocolModels(investigationId);
  const compose = useComposeAnalystOpinion(investigationId);
  const revise = useReviseAnalystOpinion(investigationId);
  const approve = useApproveAnalystOpinion(investigationId);
  const requestChanges = useRequestAnalystOpinionChanges(investigationId);
  const publish = usePublishAnalystOpinion(investigationId);
  const [blocks, setBlocks] = useState<OpinionBlockEdit[]>([]);
  const [unresolvedPoints, setUnresolvedPoints] = useState("");
  const [reviewReason, setReviewReason] = useState("");
  const [dirty, setDirty] = useState(false);
  const [newBlockType, setNewBlockType] = useState<OpinionBlockType | "">("");
  const [protocolBlobId, setProtocolBlobId] = useState("");
  const [protocolViews, setProtocolViews] = useState<ProtocolDiagramKind[]>([
    "sequence",
    "state_machine",
    "message_format",
  ]);
  const [publishedArtifact, setPublishedArtifact] = useState<
    PublishOpinionResponse["publication"] | null
  >(null);

  const draft = opinionQuery.data?.draft ?? null;
  const draftBlocks = useMemo(() => {
    if (!draft) return [];
    return BLOCK_ORDER.flatMap((type) => {
      const block = draft.blocks.find(
        (item) => item.type === type && item.text.trim(),
      );
      return block
        ? [
            {
              block_type: block.type,
              text: block.text,
              finding_refs: block.finding_refs,
              evidence_refs: block.evidence_refs,
              qualification: block.qualification,
              certainty: block.certainty,
            },
          ]
        : [];
    });
  }, [draft]);

  useEffect(() => {
    setBlocks(draftBlocks);
    setUnresolvedPoints(draft?.unresolved_points.join("\n") ?? "");
    setDirty(false);
  }, [
    draftBlocks,
    draft?.content_sha256,
    draft?.version,
    draft?.unresolved_points,
  ]);

  if (opinionQuery.isPending) return <p role="status">Chargement de l’avis…</p>;
  if (opinionQuery.isError) {
    return (
      <p className="error-message" role="alert">
        Impossible de charger l’avis : {opinionQuery.error.message}
      </p>
    );
  }

  const hashMatchesCurrent = Boolean(
    draft &&
    currentDraft &&
    draft.version === currentDraft.version &&
    draft.content_sha256 === currentDraft.content_sha256,
  );
  const citationViolations = opinionQuery.data.citation_violations;
  const error =
    compose.error ??
    revise.error ??
    approve.error ??
    requestChanges.error ??
    publish.error;
  const mutationViolations = getCitationViolations(error);
  const isReviewPending = approve.isPending || requestChanges.isPending;
  const canEdit = draft !== null && draft.status !== "approved";
  const canReview = Boolean(
    draft &&
    draft.status === "draft" &&
    hashMatchesCurrent &&
    citationViolations.length === 0 &&
    !dirty,
  );
  const canPublish = Boolean(
    draft && draft.status === "approved" && hashMatchesCurrent && !dirty,
  );
  const selectedProtocolBlobId =
    protocolBlobId || protocolModelsQuery.data?.models[0]?.blob_id || "";

  function saveRevision() {
    if (!draft) return;
    revise.mutate({
      opinionId: draft.id,
      expected_version: draft.version,
      blocks: blocks.filter((block) => block.text.trim()),
      unresolved_points: unresolvedPoints
        .split("\n")
        .map((point) => point.trim())
        .filter(Boolean),
    });
  }

  function attachProtocolFigures() {
    if (!draft || !selectedProtocolBlobId || protocolViews.length === 0) return;
    revise.mutate({
      opinionId: draft.id,
      expected_version: draft.version,
      blocks: blocks.filter((block) => block.text.trim()),
      unresolved_points: unresolvedPoints
        .split("\n")
        .map((point) => point.trim())
        .filter(Boolean),
      protocol_model_blob_id: selectedProtocolBlobId,
      protocol_views: protocolViews,
    });
  }

  function toggleProtocolView(kind: ProtocolDiagramKind) {
    setProtocolViews((current) =>
      current.includes(kind)
        ? current.filter((item) => item !== kind)
        : [...current, kind],
    );
  }

  function handleAddBlock() {
    if (
      !newBlockType ||
      blocks.some((block) => block.block_type === newBlockType)
    )
      return;
    setBlocks((current) => [
      ...current,
      {
        block_type: newBlockType,
        text: "",
        finding_refs: [],
        evidence_refs: [],
        qualification: "hypothesis",
        certainty: 0.5,
      },
    ]);
    setDirty(true);
    setNewBlockType("");
  }

  function updateBlock(index: number, update: Partial<OpinionBlockEdit>) {
    setBlocks((current) =>
      current.map((block, blockIndex) =>
        blockIndex === index ? { ...block, ...update } : block,
      ),
    );
    setDirty(true);
  }

  return (
    <section className="job-card" aria-labelledby="analyst-opinion-heading">
      <h2 id="analyst-opinion-heading">Avis de l’analyste</h2>
      <p>
        La décision est attribuée à l’identité authentifiée par le serveur. Une
        approbation vise la version et le hash affichés ci-dessous.
      </p>
      {error ? (
        <p className="error-message" role="alert">
          {error.message}
          {error instanceof InvestigationApiError
            ? ` · code : ${error.code}`
            : ""}
        </p>
      ) : null}
      {mutationViolations.length > 0 ? (
        <CitationViolations
          violations={mutationViolations}
          title="Violations de citation retournées par le serveur"
        />
      ) : null}
      {citationViolations.length > 0 ? (
        <CitationViolations
          violations={citationViolations}
          title="Violations de citation à corriger"
        />
      ) : null}

      {!draft ? (
        <div className="empty-state">
          <p>Aucun bloc d’avis n’est encore composé.</p>
          <button
            type="button"
            className="button button--secondary"
            disabled={compose.isPending}
            onClick={() => compose.mutate({})}
          >
            {compose.isPending
              ? "Composition…"
              : "Composer depuis les constats"}
          </button>
        </div>
      ) : (
        <>
          <dl>
            <div>
              <dt>Version</dt>
              <dd>
                {draft.version} · {draft.status}
              </dd>
            </div>
            <div>
              <dt>Hash de contenu</dt>
              <dd>
                <code>{draft.content_sha256}</code>
              </dd>
            </div>
            <div>
              <dt>Synthèse exacte</dt>
              <dd>
                {draft.provenance.synthesis_artifact_id} ·{" "}
                {draft.provenance.synthesis_sha256}
              </dd>
            </div>
            <div>
              <dt>PUBLICATION de base</dt>
              <dd>
                {draft.provenance.base_publication_artifact_id} ·{" "}
                {draft.provenance.base_publication_sha256}
              </dd>
            </div>
          </dl>
          {!hashMatchesCurrent ? (
            <p className="verification-warning" role="alert">
              La version ou le hash ne correspond plus au brouillon courant de
              l’enquête. Rechargez avant toute décision.
            </p>
          ) : null}
          {dirty ? (
            <p role="status">
              Modifications locales non enregistrées. Enregistrez-les avant la
              revue.
            </p>
          ) : null}

          <fieldset disabled={!canEdit}>
            <legend>Édition par bloc · blocs renseignés seulement</legend>
            {blocks.filter((block) => block.text.trim() || dirty).length ===
            0 ? (
              <p>Aucun bloc renseigné.</p>
            ) : null}
            {blocks.map((block, index) => (
              <section
                key={block.block_type}
                aria-labelledby={`opinion-block-${block.block_type}`}
              >
                <h3 id={`opinion-block-${block.block_type}`}>
                  {BLOCK_LABELS[block.block_type]}
                </h3>
                <label>
                  Texte du bloc
                  <textarea
                    value={block.text}
                    onChange={(event) =>
                      updateBlock(index, { text: event.currentTarget.value })
                    }
                  />
                </label>
                <label>
                  Qualification
                  <select
                    value={block.qualification}
                    onChange={(event) =>
                      updateBlock(index, {
                        qualification: event.currentTarget
                          .value as OpinionQualification,
                      })
                    }
                  >
                    {Object.entries(QUALIFICATION_LABELS).map(
                      ([value, label]) => (
                        <option key={value} value={value}>
                          {label}
                        </option>
                      ),
                    )}
                  </select>
                </label>
                <label>
                  Niveau de confiance : {Math.round(block.certainty * 100)} %
                  <input
                    type="range"
                    min="0"
                    max="1"
                    step="0.05"
                    value={block.certainty}
                    onChange={(event) =>
                      updateBlock(index, {
                        certainty: Number(event.currentTarget.value),
                      })
                    }
                  />
                </label>
                <p>
                  Findings cités : {block.finding_refs.join(", ") || "aucun"}
                </p>
                <p>
                  Preuves citées : {block.evidence_refs.join(", ") || "aucune"}
                </p>
                {canEdit ? (
                  <button
                    type="button"
                    className="button button--secondary"
                    onClick={() => {
                      setBlocks((current) =>
                        current.filter((_, blockIndex) => blockIndex !== index),
                      );
                      setDirty(true);
                    }}
                  >
                    Retirer ce bloc
                  </button>
                ) : null}
              </section>
            ))}
            {canEdit ? (
              <div>
                <label>
                  Ajouter un bloc facultatif
                  <select
                    value={newBlockType}
                    onChange={(event) =>
                      setNewBlockType(
                        event.currentTarget.value as OpinionBlockType | "",
                      )
                    }
                  >
                    <option value="">Choisir un type</option>
                    {BLOCK_ORDER.filter(
                      (type) =>
                        !blocks.some((block) => block.block_type === type),
                    ).map((type) => (
                      <option key={type} value={type}>
                        {BLOCK_LABELS[type]}
                      </option>
                    ))}
                  </select>
                </label>
                <button
                  type="button"
                  className="button button--secondary"
                  disabled={!newBlockType}
                  onClick={handleAddBlock}
                >
                  Ajouter
                </button>
              </div>
            ) : null}
            <label>
              Points non résolus (un par ligne)
              <textarea
                value={unresolvedPoints}
                onChange={(event) => {
                  setUnresolvedPoints(event.currentTarget.value);
                  setDirty(true);
                }}
                disabled={!canEdit}
              />
            </label>
            <fieldset disabled={!canEdit || protocolModelsQuery.isPending}>
              <legend>
                Figures du protocole · attachées à la prochaine version
              </legend>
              {protocolModelsQuery.isError ? (
                <p className="error-message" role="alert">
                  Les modèles de protocole ne peuvent pas être chargés :{" "}
                  {protocolModelsQuery.error.message}
                </p>
              ) : protocolModelsQuery.data?.models.length ? (
                <>
                  <label htmlFor="opinion-protocol-model">Modèle source</label>
                  <select
                    id="opinion-protocol-model"
                    value={selectedProtocolBlobId}
                    onChange={(event) =>
                      setProtocolBlobId(event.currentTarget.value)
                    }
                  >
                    {protocolModelsQuery.data.models.map((model) => (
                      <option key={model.blob_id} value={model.blob_id}>
                        {model.protocol ?? model.transport} ·{" "}
                        {PROTOCOL_STATUS_LABELS[model.validation_status] ??
                          model.validation_status}{" "}
                        · SHA {model.sha256.slice(0, 12)}
                      </option>
                    ))}
                  </select>
                  <div>
                    <p>Vues à attacher</p>
                    {(
                      [
                        ["sequence", "Séquence des échanges"],
                        ["state_machine", "Machine à états"],
                        ["message_format", "Format des messages"],
                      ] as const
                    ).map(([kind, label]) => (
                      <label key={kind}>
                        <input
                          type="checkbox"
                          checked={protocolViews.includes(kind)}
                          onChange={() => toggleProtocolView(kind)}
                        />{" "}
                        {label}
                      </label>
                    ))}
                  </div>
                  <p role="note">
                    L’attache crée une nouvelle version de l’avis. Les
                    modifications locales sont enregistrées avec les figures.
                  </p>
                  <button
                    type="button"
                    className="button button--secondary"
                    disabled={
                      !selectedProtocolBlobId ||
                      protocolViews.length === 0 ||
                      revise.isPending
                    }
                    onClick={attachProtocolFigures}
                  >
                    {revise.isPending
                      ? "Compilation et attache…"
                      : "Attacher les figures à une nouvelle révision"}
                  </button>
                </>
              ) : protocolModelsQuery.isPending ? (
                <p role="status">Chargement des modèles de protocole…</p>
              ) : (
                <p>
                  Aucun modèle de protocole n’est disponible pour cette enquête.
                </p>
              )}
            </fieldset>
            {draft.protocol_figures?.length ? (
              <div>
                <h3>Figures déjà liées à cette version</h3>
                <ul>
                  {draft.protocol_figures.map((figure) => (
                    <li key={`${figure.kind}-${figure.asset_sha256}`}>
                      {figure.kind} · <code>{figure.asset_sha256}</code> ·{" "}
                      {figure.caption}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
            {canEdit ? (
              <button
                type="button"
                className="button button--secondary"
                disabled={!dirty || revise.isPending}
                onClick={saveRevision}
              >
                {revise.isPending
                  ? "Enregistrement…"
                  : `Enregistrer la révision (CAS v${draft.version})`}
              </button>
            ) : null}
          </fieldset>

          {draft.status === "draft" ? (
            <fieldset>
              <legend>Revue de la version précise</legend>
              <label>
                Raison de la décision
                <textarea
                  value={reviewReason}
                  onChange={(event) =>
                    setReviewReason(event.currentTarget.value)
                  }
                />
              </label>
              <p>
                Version {draft.version} · SHA-256{" "}
                <code>{draft.content_sha256}</code>
              </p>
              <button
                type="button"
                className="button button--secondary"
                disabled={!canReview || !reviewReason.trim() || isReviewPending}
                onClick={() =>
                  approve.mutate({
                    opinionId: draft.id,
                    version: draft.version,
                    expected_sha256: draft.content_sha256,
                    reason: reviewReason,
                  })
                }
              >
                Approuver cette version précise
              </button>{" "}
              <button
                type="button"
                className="button button--secondary"
                disabled={!canReview || !reviewReason.trim() || isReviewPending}
                onClick={() =>
                  requestChanges.mutate({
                    opinionId: draft.id,
                    version: draft.version,
                    expected_sha256: draft.content_sha256,
                    reason: reviewReason,
                  })
                }
              >
                Demander des modifications
              </button>
            </fieldset>
          ) : null}

          {draft.status === "approved" ? (
            <section aria-labelledby="publish-opinion-heading">
              <h3 id="publish-opinion-heading">
                Proposer une version augmentée de l’article
              </h3>
              <p>
                La publication créera un nouvel artefact PUBLICATION V6. Le
                réépinglage dans une composition reste une action humaine
                distincte.
              </p>
              <button
                type="button"
                className="button button--secondary"
                disabled={!canPublish || publish.isPending}
                onClick={() =>
                  publish.mutate(
                    {
                      opinionId: draft.id,
                      version: draft.version,
                      expected_base_publication_sha256:
                        draft.provenance.base_publication_sha256,
                    },
                    {
                      onSuccess: (result) =>
                        setPublishedArtifact(result.publication),
                    },
                  )
                }
              >
                {publish.isPending
                  ? "Publication…"
                  : "Proposer la version augmentée"}
              </button>
            </section>
          ) : null}

          {publishedArtifact ? (
            <section
              className="verification-warning"
              role="status"
              aria-live="polite"
            >
              <h3>Version augmentée proposée</h3>
              <p>
                PUBLICATION {publishedArtifact.artifact_id} · version{" "}
                {publishedArtifact.version} · SHA-256{" "}
                <code>{publishedArtifact.sha256}</code>
              </p>
              <p>
                Repin à faire dans la composition : cette publication ne modifie
                pas le pin courant.
              </p>
              <a href={compositionHref}>
                Ouvrir la composition pour réépingler l’article
              </a>
            </section>
          ) : null}

          {opinionQuery.data.history.length > 1 ? (
            <details>
              <summary>Historique des versions de l’avis</summary>
              <ol>
                {opinionQuery.data.history.map((item) => (
                  <li key={`${item.id}-${item.version}`}>
                    Version {item.version} · {item.status} · SHA-256{" "}
                    <code>{item.content_sha256}</code>
                  </li>
                ))}
              </ol>
            </details>
          ) : null}
        </>
      )}
      <p>
        Le contenu de cet avis ne conditionne pas la publiabilité des autres
        sujets.
      </p>
    </section>
  );
}

function CitationViolations({
  violations,
  title,
}: {
  violations: CitationViolationPayload[];
  title: string;
}) {
  return (
    <section className="error-message" aria-label={title} role="alert">
      <h3>{title}</h3>
      <ul>
        {violations.map((violation, index) => (
          <li
            key={`${violation.code}-${violation.reference_id ?? "none"}-${index}`}
          >
            {violation.code} · {violation.block_type} · {violation.message}
            {violation.reference_id
              ? ` · référence ${violation.reference_id}`
              : ""}
          </li>
        ))}
      </ul>
    </section>
  );
}

function getCitationViolations(error: unknown): CitationViolationPayload[] {
  if (!(error instanceof InvestigationApiError) || !error.details) return [];
  const raw = error.details.citation_violations;
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((item): CitationViolationPayload[] => {
    if (typeof item !== "object" || item === null) return [];
    const candidate = item as Record<string, unknown>;
    if (
      typeof candidate.code !== "string" ||
      typeof candidate.block_type !== "string" ||
      typeof candidate.message !== "string"
    ) {
      return [];
    }
    return [
      {
        code: candidate.code,
        block_type: candidate.block_type,
        message: candidate.message,
        reference_id:
          typeof candidate.reference_id === "string"
            ? candidate.reference_id
            : null,
      },
    ];
  });
}
