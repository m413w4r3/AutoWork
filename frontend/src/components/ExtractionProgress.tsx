import type {
  ExtractionProgress,
  ExtractionProgressProfile,
  ExtractionProgressSource,
  ExtractionProgressSourceStatus,
} from "../api/production";

const PROFILE_LABELS: Record<ExtractionProgressProfile, string> = {
  full: "FULL",
  ioc_rules: "IOC uniquement",
};

const TIER_LABELS: Record<ExtractionProgressSource["tier"], string> = {
  core: "CORE",
  supporting: "Complémentaire",
  technical: "Technique",
};

const SOURCE_STATUS_LABELS: Record<ExtractionProgressSourceStatus, string> = {
  pending: "En attente",
  cached: "Résultat existant",
  succeeded: "Terminé",
  failed: "Échec",
  omitted: "Non éligible dans le corpus",
};

const SOURCE_STATUS_ICONS: Record<ExtractionProgressSourceStatus, string> = {
  pending: "○",
  cached: "✓",
  succeeded: "✓",
  failed: "×",
  omitted: "–",
};

function statusLabel(source: ExtractionProgressSource): string {
  const label = SOURCE_STATUS_LABELS[source.status];
  return source.reuse_state === "content_duplicate"
    ? `${label} · contenu identique à une autre source`
    : label;
}

export function ExtractionProgressView({
  progress,
}: {
  progress: ExtractionProgress;
}) {
  return (
    <section
      className="extraction-progress"
      aria-label="Progression de l’extraction"
    >
      <div className="extraction-progress__heading">
        <strong>
          Extraction {progress.completed_sources} / {progress.total_sources}
        </strong>
        <span>
          FULL {progress.full_completed} / {progress.full_total}
        </span>
        <span>
          IOC uniquement {progress.ioc_rules_completed} /{" "}
          {progress.ioc_rules_total}
        </span>
      </div>

      <div className="extraction-progress__counts">
        <span>
          IOCs : {progress.confirmed_iocs} confirmés ·{" "}
          {progress.contextual_iocs} contextuels
        </span>
        <span>
          Règles : {progress.rules_total} · YARA {progress.yara_rules} · Sigma{" "}
          {progress.sigma_rules} · Suricata {progress.suricata_rules} · Snort{" "}
          {progress.snort_rules}
        </span>
        <span>
          Résultats existants : {progress.cache_hits} · Appels modèle :{" "}
          {progress.model_calls}
        </span>
      </div>

      <ul
        className="extraction-progress__sources"
        aria-label="Sources de l’extraction"
      >
        {progress.sources.map((source) => (
          <li key={source.source_id} className={`is-${source.status}`}>
            <span aria-hidden="true">{SOURCE_STATUS_ICONS[source.status]}</span>
            <span>{source.title?.trim() || source.canonical_url}</span>
            <span>
              {TIER_LABELS[source.tier]}
              {source.profile ? ` · ${PROFILE_LABELS[source.profile]}` : ""}
            </span>
            <span className="extraction-progress__source-status">
              {statusLabel(source)}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
