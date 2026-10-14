import { useState } from "react";

import {
  useInvestigationHypotheses,
  useInvestigationRetrohunt,
  useProtocolDiagrams,
  useProtocolModel,
  useProtocolModels,
  useSampleComparison,
  useSampleComparisons,
  type ProtocolDiagramKind,
  type ProtocolModelDetail,
  type SampleComparisonFeature,
} from "../../api/investigation";

const COMPARISON_STATUS: Record<string, string> = {
  complete: "Comparaison complète",
  partial_feature_coverage: "Couverture partielle",
  insufficient_comparative_support: "Support comparatif insuffisant",
  feature_packs_unavailable: "Traits indisponibles",
  no_comparable_features: "Aucun trait comparable",
};

const FEATURE_CLASS: Record<string, string> = {
  core_strongly_shared: "Noyau fortement partagé",
  subfamily_discriminant: "Discriminant de sous-famille",
  optional: "Facultatif",
  divergence_explained_by_version_or_packing:
    "Divergence version/packing expliquée",
  library_or_packer_contamination: "Contamination bibliothèque/packer",
};

const HYPOTHESIS_STATUS: Record<string, string> = {
  open: "Ouverte",
  supported: "Étayée",
  refuted: "Réfutée",
  abandoned: "Abandonnée",
};

const VALIDATION_STATUS: Record<string, string> = {
  observed: "Observé",
  inferred: "Inféré",
  unresolved: "Non résolu",
  contradicted: "Contredit",
};

const DIAGRAM_KIND: Record<ProtocolDiagramKind, string> = {
  sequence: "Séquence des échanges",
  state_machine: "Machine à états",
  message_format: "Format des messages",
};

function StatusBadge({
  value,
  labels,
}: {
  value: string;
  labels: Record<string, string>;
}) {
  return (
    <span className="badge">{labels[value] ?? value.replaceAll("_", " ")}</span>
  );
}

export function InvestigationReadPanels({
  investigationId,
}: {
  investigationId: string;
}) {
  return (
    <>
      <SampleComparisonPanel investigationId={investigationId} />
      <HypothesisEvidencePanel investigationId={investigationId} />
      <ProtocolPanel investigationId={investigationId} />
      <RetrohuntPanel investigationId={investigationId} />
    </>
  );
}

function SampleComparisonPanel({
  investigationId,
}: {
  investigationId: string;
}) {
  const comparisonsQuery = useSampleComparisons(investigationId);
  const [selectedBlobId, setSelectedBlobId] = useState("");
  const blobId =
    selectedBlobId || comparisonsQuery.data?.comparisons[0]?.blob_id || "";
  const detailQuery = useSampleComparison(investigationId, blobId);

  return (
    <section className="job-card" aria-labelledby="sample-comparison-heading">
      <h2 id="sample-comparison-heading">Comparaison d’échantillons</h2>
      {comparisonsQuery.isPending ? (
        <p role="status">Chargement des comparaisons…</p>
      ) : comparisonsQuery.isError ? (
        <p className="error-message" role="alert">
          Les comparaisons sont indisponibles : {comparisonsQuery.error.message}
        </p>
      ) : comparisonsQuery.data.comparisons.length === 0 ? (
        <p>
          Aucune comparaison de caractéristiques n’est disponible pour cette
          enquête.
        </p>
      ) : (
        <>
          <label htmlFor="sample-comparison-select">
            Comparaison enregistrée
          </label>
          <select
            id="sample-comparison-select"
            value={blobId}
            onChange={(event) => setSelectedBlobId(event.currentTarget.value)}
          >
            {comparisonsQuery.data.comparisons.map((comparison) => (
              <option key={comparison.blob_id} value={comparison.blob_id}>
                {COMPARISON_STATUS[comparison.status] ?? comparison.status} ·{" "}
                {comparison.sample_count} échantillon(s) ·{" "}
                {comparison.feature_count} traits
              </option>
            ))}
          </select>
          {detailQuery.isPending ? (
            <p role="status">Lecture des caractéristiques mesurées…</p>
          ) : detailQuery.isError ? (
            <p className="error-message" role="alert">
              Le détail de cette comparaison est indisponible :{" "}
              {detailQuery.error.message}
            </p>
          ) : (
            <>
              <p>
                <StatusBadge
                  value={detailQuery.data.status}
                  labels={COMPARISON_STATUS}
                />{" "}
                SHA-256 de la comparaison :{" "}
                <code>{detailQuery.data.sha256}</code>
              </p>
              <p>
                Échantillons comparés : {detailQuery.data.sample_sha256s.length}{" "}
                · traits mesurés : {detailQuery.data.feature_count}
              </p>
              <ComparisonLimits limits={detailQuery.data.limits} />
              <div className="table-scroll">
                <table>
                  <caption>
                    Support, prévalence goodware et classe des traits
                  </caption>
                  <thead>
                    <tr>
                      <th scope="col">Trait</th>
                      <th scope="col">Support</th>
                      <th scope="col">Présence goodware</th>
                      <th scope="col">Classe</th>
                      <th scope="col">Limites et provenance</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detailQuery.data.features.map((feature) => (
                      <ComparisonFeatureRow
                        key={feature.feature_key}
                        feature={feature}
                      />
                    ))}
                  </tbody>
                </table>
              </div>
              {detailQuery.data.features.length === 0 ? (
                <p>Aucun trait comparable n’a été mesuré.</p>
              ) : null}
              {detailQuery.data.features_truncated ? (
                <p role="note">
                  La liste des traits est limitée pour cet affichage.
                </p>
              ) : null}
              {detailQuery.data.warnings.length > 0 ? (
                <div role="note">
                  <strong>Limites</strong>
                  <ul>
                    {detailQuery.data.warnings.map((warning) => (
                      <li key={warning}>{warning}</li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </>
          )}
        </>
      )}
    </section>
  );
}

function ComparisonLimits({ limits }: { limits: Record<string, unknown> }) {
  const selected = numericField(limits, "selected_samples");
  const corpus = numericField(limits, "corpus_samples");
  const features = numericField(limits, "features");
  const missing = limits.packs_missing;
  const missingCount = Array.isArray(missing) ? missing.length : 0;
  return (
    <p role="note">
      Limites de mesure : {selected ?? "?"} échantillon(s) sélectionné(s),{" "}
      {corpus ?? "?"} du corpus et {features ?? "?"} trait(s)
      {missingCount > 0
        ? ` · ${missingCount} pack(s) de caractéristiques absent(s)`
        : ""}
      {limits.corpus_sample_limit_applied === true ? " · corpus tronqué" : ""}
      {limits.feature_limit_applied === true
        ? " · nombre de traits plafonné"
        : ""}
      .
    </p>
  );
}

function ComparisonFeatureRow({
  feature,
}: {
  feature: SampleComparisonFeature;
}) {
  const support = feature.support;
  const provenance = feature.sample_presence
    .flatMap((presence) => presence.sources)
    .map((source) => {
      const kind = source.source_kind;
      const version = source.extractor_version;
      return [kind, version]
        .filter((value): value is string => typeof value === "string")
        .join(" ");
    })
    .filter(Boolean);
  const locations = feature.sample_presence.flatMap((presence) => [
    ...presence.offsets.map(
      (offset) =>
        `${presence.sample_sha256.slice(0, 12)} +0x${offset.toString(16)}`,
    ),
    ...presence.function_offsets.map(
      (offset) =>
        `${presence.sample_sha256.slice(0, 12)} fonction +0x${offset.toString(16)}`,
    ),
  ]);
  return (
    <tr>
      <th scope="row">
        <code>{feature.value || feature.kind}</code>
        {feature.encoding ? <small> · {feature.encoding}</small> : null}
      </th>
      <td>
        {support.positive}/{support.positive_sample_count} positifs ·{" "}
        {support.corpus}/{support.corpus_sample_count} corpus
      </td>
      <td>
        {support.goodware_prevalence_status === "measured" &&
        support.goodware_occurrence_count !== null
          ? `${support.goodware_occurrence_count} occurrence(s) mesurée(s)`
          : "Non mesurée"}
      </td>
      <td>
        <StatusBadge value={feature.feature_class} labels={FEATURE_CLASS} />
      </td>
      <td>
        {feature.rationale.length > 0 ? (
          <p>{feature.rationale.join(" · ")}</p>
        ) : null}
        {feature.rejection_reasons.length > 0 ? (
          <p>Réserves : {feature.rejection_reasons.join(", ")}</p>
        ) : null}
        {provenance.length > 0 ? (
          <p>Sources : {[...new Set(provenance)].join(", ")}</p>
        ) : null}
        {locations.length > 0 ? (
          <p>Positions : {locations.slice(0, 12).join(" · ")}</p>
        ) : null}
      </td>
    </tr>
  );
}

function HypothesisEvidencePanel({
  investigationId,
}: {
  investigationId: string;
}) {
  const query = useInvestigationHypotheses(investigationId);
  return (
    <section className="job-card" aria-labelledby="hypothesis-evidence-heading">
      <h2 id="hypothesis-evidence-heading">Preuve → hypothèse</h2>
      {query.isPending ? (
        <p role="status">Chargement des hypothèses et des preuves liées…</p>
      ) : query.isError ? (
        <p className="error-message" role="alert">
          Les liens preuve–hypothèse sont indisponibles : {query.error.message}
        </p>
      ) : query.data.hypotheses.length === 0 ? (
        <p>Aucune hypothèse n’est enregistrée dans cette enquête.</p>
      ) : (
        <ul>
          {query.data.hypotheses.map((hypothesis) => (
            <li key={hypothesis.id}>
              <h3>{hypothesis.statement}</h3>
              <StatusBadge
                value={hypothesis.status}
                labels={HYPOTHESIS_STATUS}
              />
              {hypothesis.findings.length > 0 ? (
                <div>
                  <h4>Constats liés</h4>
                  <ul>
                    {hypothesis.findings.map((finding) => (
                      <li key={finding.id}>
                        <StatusBadge
                          value={finding.assessment}
                          labels={{
                            supported: "Étayé",
                            refuted: "Réfuté",
                            qualified: "Nuancé",
                            inconclusive: "Non établi",
                          }}
                        />{" "}
                        {finding.statement}
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
              {hypothesis.observations.length > 0 ? (
                <div>
                  <h4>Observations liées</h4>
                  <ul>
                    {hypothesis.observations.map((observation) => (
                      <li key={observation.id}>
                        {observation.source} · {observation.kind} ·{" "}
                        <code>{observation.sha256.slice(0, 16)}</code>
                        {observation.limitations.length > 0
                          ? ` · ${observation.limitations.join(" ; ")}`
                          : ""}
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
              {hypothesis.contradictions.length > 0 ? (
                <div role="note" className="verification-warning">
                  <h4>Contradictions conservées</h4>
                  <ul>
                    {hypothesis.contradictions.map((item) => (
                      <li key={item.id}>{item.statement}</li>
                    ))}
                  </ul>
                </div>
              ) : null}
              {hypothesis.findings.length === 0 &&
              hypothesis.observations.length === 0 ? (
                <p>Aucune preuve liée n’est disponible pour l’instant.</p>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function ProtocolPanel({ investigationId }: { investigationId: string }) {
  const modelsQuery = useProtocolModels(investigationId);
  const [selectedBlobId, setSelectedBlobId] = useState("");
  const blobId = selectedBlobId || modelsQuery.data?.models[0]?.blob_id || "";
  const modelQuery = useProtocolModel(investigationId, blobId);
  const diagramsQuery = useProtocolDiagrams(investigationId, blobId);

  return (
    <section className="job-card" aria-labelledby="protocol-model-heading">
      <h2 id="protocol-model-heading">Protocole réseau</h2>
      {modelsQuery.isPending ? (
        <p role="status">Chargement des modèles de protocole…</p>
      ) : modelsQuery.isError ? (
        <p className="error-message" role="alert">
          Les modèles de protocole sont indisponibles :{" "}
          {modelsQuery.error.message}
        </p>
      ) : modelsQuery.data.models.length === 0 ? (
        <p>Aucun modèle de protocole n’est disponible dans cette enquête.</p>
      ) : (
        <>
          <label htmlFor="protocol-model-select">Modèle de protocole</label>
          <select
            id="protocol-model-select"
            value={blobId}
            onChange={(event) => setSelectedBlobId(event.currentTarget.value)}
          >
            {modelsQuery.data.models.map((model) => (
              <option key={model.blob_id} value={model.blob_id}>
                {model.protocol ?? model.transport} ·{" "}
                {VALIDATION_STATUS[model.validation_status] ??
                  model.validation_status}{" "}
                · {model.message_count} message(s)
              </option>
            ))}
          </select>
          {modelQuery.isPending ? (
            <p role="status">Lecture du modèle…</p>
          ) : modelQuery.isError ? (
            <p className="error-message" role="alert">
              Le modèle sélectionné est indisponible :{" "}
              {modelQuery.error.message}
            </p>
          ) : (
            <>
              <p>
                {stringField(modelQuery.data.model, "protocol") ??
                  "Protocole non identifié"}{" "}
                ·{" "}
                {stringField(modelQuery.data.model, "transport") ??
                  "Transport inconnu"}{" "}
                ·{" "}
                <StatusBadge
                  value={
                    stringField(modelQuery.data.model, "validation_status") ??
                    "unresolved"
                  }
                  labels={VALIDATION_STATUS}
                />
              </p>
              <ProtocolDefinitions model={modelQuery.data.model} />
            </>
          )}
          {diagramsQuery.isPending ? (
            <p role="status">Compilation des trois prévisualisations…</p>
          ) : diagramsQuery.isError ? (
            <p className="error-message" role="alert">
              Les diagrammes ne peuvent pas être prévisualisés :{" "}
              {diagramsQuery.error.message}
            </p>
          ) : (
            <div>
              <h3>Prévisualisations des diagrammes</h3>
              {diagramsQuery.data.diagrams.map((diagram) => (
                <article
                  key={diagram.kind}
                  aria-labelledby={`protocol-diagram-${diagram.kind}`}
                >
                  <h4 id={`protocol-diagram-${diagram.kind}`}>
                    {DIAGRAM_KIND[diagram.kind]}
                  </h4>
                  <StatusBadge
                    value={diagram.status}
                    labels={{
                      ready: "Disponible",
                      insufficient_evidence: "Éléments insuffisants",
                    }}
                  />
                  {diagram.status === "insufficient_evidence" ? (
                    <p role="note">
                      Éléments insuffisants : aucune séquence générique n’est
                      présentée comme observée.
                    </p>
                  ) : null}
                  <img
                    className="investigation-protocol-preview"
                    src={diagram.png_preview.data_url}
                    alt={`${DIAGRAM_KIND[diagram.kind]} : ${diagram.title}`}
                  />
                  <p>
                    SHA-256 SVG : <code>{diagram.asset_sha256}</code>{" "}
                    <a
                      href={diagram.svg.data_url}
                      download={`${diagram.kind}.svg`}
                    >
                      Ouvrir le SVG
                    </a>
                  </p>
                  {diagram.limitations.length > 0 ? (
                    <p>Limites : {diagram.limitations.join(" · ")}</p>
                  ) : null}
                </article>
              ))}
            </div>
          )}
        </>
      )}
    </section>
  );
}

function ProtocolDefinitions({
  model,
}: {
  model: ProtocolModelDetail["model"];
}) {
  return (
    <div>
      <h3>Messages et champs</h3>
      {model.messages.length === 0 ? (
        <p>Aucun message établi.</p>
      ) : (
        <ul>
          {model.messages.map((message, index) => (
            <li key={stringField(message, "message_id") ?? index}>
              <strong>
                {stringField(message, "name") ?? "Message sans nom"}
              </strong>{" "}
              <StatusBadge
                value={
                  stringField(message, "validation_status") ?? "unresolved"
                }
                labels={VALIDATION_STATUS}
              />
              <span>
                {" "}
                · longueur {numberField(message, "min_length") ?? "?"}–
                {numberField(message, "max_length") ?? "?"} octets
              </span>
            </li>
          ))}
        </ul>
      )}
      {model.fields.length === 0 ? (
        <p>Aucun champ établi.</p>
      ) : (
        <ul>
          {model.fields.map((field, index) => (
            <li key={stringField(field, "field_id") ?? index}>
              <strong>{stringField(field, "name") ?? "Champ sans nom"}</strong>{" "}
              <StatusBadge
                value={stringField(field, "validation_status") ?? "unresolved"}
                labels={VALIDATION_STATUS}
              />
              <span>
                {" "}
                · offset {numberField(field, "offset") ?? "inconnu"} · longueur{" "}
                {numberField(field, "length") ?? "inconnue"} ·{" "}
                {stringField(field, "endianness") ?? "endianness inconnue"}
              </span>
            </li>
          ))}
        </ul>
      )}
      {model.limitations.length > 0 ? (
        <p>Limites du modèle : {model.limitations.join(" · ")}</p>
      ) : null}
    </div>
  );
}

function stringField(
  value: Record<string, unknown>,
  key: string,
): string | null {
  const item = value[key];
  return typeof item === "string" ? item : null;
}

function numberField(
  value: Record<string, unknown>,
  key: string,
): number | null {
  const item = value[key];
  return typeof item === "number" ? item : null;
}

function numericField(
  value: Record<string, unknown>,
  key: string,
): number | null {
  const item = value[key];
  return typeof item === "number" ? item : null;
}

function RetrohuntPanel({ investigationId }: { investigationId: string }) {
  const query = useInvestigationRetrohunt(investigationId);
  return (
    <section className="job-card" aria-labelledby="retrohunt-heading">
      <h2 id="retrohunt-heading">Retrohunt VirusTotal</h2>
      {query.isPending ? (
        <p role="status">Chargement du quota Retrohunt…</p>
      ) : query.isError ? (
        <p className="error-message" role="alert">
          Le compteur Retrohunt est indisponible : {query.error.message}
        </p>
      ) : (
        <>
          <p>
            <strong>
              {query.data.consumed_slots}/{query.data.maximum_slots}{" "}
              emplacements consommés
            </strong>{" "}
            · {query.data.remaining_slots} restant(s) pour le sujet.
          </p>
          {query.data.jobs.length === 0 ? (
            <p>Aucun job Retrohunt enregistré pour cette enquête.</p>
          ) : (
            <ul>
              {query.data.jobs.map((job) => (
                <li key={job.reservation_id}>
                  Emplacement {job.slot} · état :{" "}
                  <StatusBadge
                    value={job.status}
                    labels={{
                      reserved: "Réservé",
                      submitting: "Soumission en cours",
                      submitted: "Soumis",
                      reconciliation_required: "Réconciliation requise",
                      completed: "Terminé",
                      failed: "Échec",
                    }}
                  />
                  {job.has_provider_job
                    ? " · job fournisseur associé"
                    : " · aucun identifiant de job fournisseur"}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
