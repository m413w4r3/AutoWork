import type { ProductionSourceExtractionV1 } from "../../api/production";
import {
  REFERENCE_KIND_LABELS,
  REFERENCE_ROLE_LABELS,
  REFERENCE_TIER_LABELS,
  REUSE_STATE_LABELS,
} from "./productionStageLabels";

function ProductionExtractionSourceCard({
  source,
}: {
  source: ProductionSourceExtractionV1;
}) {
  return (
    <li>
      <article className="extraction-source">
        <h4>
          <a href={source.canonical_url}>{source.canonical_url}</a>
        </h4>
        <dl>
          <div>
            <dt>Tier</dt>
            <dd>{REFERENCE_TIER_LABELS[source.tier]}</dd>
          </div>
          <div>
            <dt>Type de source</dt>
            <dd>{REFERENCE_KIND_LABELS[source.kind]}</dd>
          </div>
          <div>
            <dt>Rôle</dt>
            <dd>{REFERENCE_ROLE_LABELS[source.role] ?? source.role}</dd>
          </div>
          <div>
            <dt>Profil</dt>
            <dd>{source.profile === "full" ? "FULL" : "IOC_RULES"}</dd>
          </div>
          <div>
            <dt>SHA-256</dt>
            <dd title={source.content_sha256}>
              {source.content_sha256.slice(0, 12)}…
            </dd>
          </div>
          <div>
            <dt>Statut</dt>
            <dd>{REUSE_STATE_LABELS[source.reuse_state]}</dd>
          </div>
          {source.scope ? (
            <div>
              <dt>Cadrage</dt>
              <dd>
                Cas {source.scope.case_id} · {source.scope.kept_sections}/
                {source.scope.total_sections} sections ·{" "}
                {source.scope.kept_chars}/{source.scope.total_chars} caractères
              </dd>
            </div>
          ) : null}
          <div>
            <dt>Faits</dt>
            <dd>{source.facts.length}</dd>
          </div>
          <div>
            <dt>Événements</dt>
            <dd>{source.events.length}</dd>
          </div>
          <div>
            <dt>IOC / artefacts</dt>
            <dd>{source.indicators.length}</dd>
          </div>
          <div>
            <dt>Règles</dt>
            <dd>{source.rules.length}</dd>
          </div>
        </dl>
      </article>
    </li>
  );
}

export function ProductionExtractionSourceList({
  sources,
  empty,
}: {
  sources: ProductionSourceExtractionV1[];
  empty: string;
}) {
  if (sources.length === 0) return <p>{empty}</p>;
  return (
    <ul className="extraction-sources">
      {sources.map((source) => (
        <ProductionExtractionSourceCard
          key={source.source_document_id}
          source={source}
        />
      ))}
    </ul>
  );
}
