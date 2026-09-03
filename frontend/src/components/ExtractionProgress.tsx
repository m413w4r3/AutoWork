import type {
  ExtractionProgress,
  ExtractionProgressProfile,
  ExtractionProgressSourceStatus,
} from "../api/production";

const PROFILE_LABELS: Record<ExtractionProgressProfile, string> = {
  full: "FULL",
  ioc_rules: "IOC uniquement",
};

const SOURCE_STATUS_LABELS: Record<ExtractionProgressSourceStatus, string> = {
  pending: "En attente",
  running: "En cours",
  cached: "Résultat existant",
  succeeded: "Terminé",
  needs_review: "À vérifier",
  failed: "Échec",
  skipped: "Ignorée (non bloquante)",
};

const SOURCE_STATUS_ICONS: Record<ExtractionProgressSourceStatus, string> = {
  pending: "○",
  running: "●",
  cached: "✓",
  succeeded: "✓",
  needs_review: "!",
  failed: "×",
  skipped: "–",
};

function profileLabel(profile: ExtractionProgressProfile | null): string {
  return profile ? PROFILE_LABELS[profile] : "";
}

function usesArchiveFallback(source: {
  access_mode?: "live_url" | "archive_fallback" | null;
  archive_fallback?: boolean;
}): boolean {
  return (
    source.access_mode === "archive_fallback" ||
    source.archive_fallback === true
  );
}

export function ExtractionProgressView({
  progress,
}: {
  progress: ExtractionProgress;
}) {
  const activeSource = progress.sources.find(
    (source) => source.source_id === progress.active_source_id,
  );
  const activeTitle = progress.active_source_title || activeSource?.title;
  const activeProfile =
    progress.active_profile || activeSource?.profile || null;

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

      {progress.active_source_id ? (
        <p className="extraction-progress__active">
          Active : <strong>{progress.active_source_id}</strong>
          {activeTitle ? ` — ${activeTitle}` : ""}
          {activeProfile ? ` · ${profileLabel(activeProfile)}` : ""}
        </p>
      ) : null}

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
            <span>{source.source_id}</span>
            <span>{PROFILE_LABELS[source.profile]}</span>
            <span className="extraction-progress__source-status">
              {SOURCE_STATUS_LABELS[source.status]}
              {usesArchiveFallback(source) ? " · Archive de secours" : ""}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
