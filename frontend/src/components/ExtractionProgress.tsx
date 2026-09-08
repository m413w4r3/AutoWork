import type {
  ExtractionProgress,
  ExtractionProgressProfile,
  ExtractionProgressSource,
  ExtractionProgressSourceStatus,
} from "../api/production";

// Why the planner decided to read a source, or to keep an existing result.
// The desk used to show only the cost, so a legitimate first extraction was
// indistinguishable from a checkpoint the pipeline failed to reuse.
const PLAN_REASON_LABELS: Record<string, string> = {
  reusable_checkpoint: "résultat existant réutilisé",
  legacy_checkpoint_recovered: "résultat archivé récupéré",
  same_content_as_primary_source: "contenu identique à une autre source",
  // NE PAS lire « jamais extraite » : une extraction antérieure au registre
  // par source n’a laissé aucune empreinte adressée par contenu, donc elle
  // est invisible ici. Le fait certain est l’absence de résultat réutilisable.
  no_checkpoint: "aucun résultat réutilisable",
  source_content_changed: "contenu réarchivé différent",
  prompt_version_changed: "version de prompt changée",
  model_policy_changed: "politique modèle changée",
  routing_policy_changed: "politique de routage changée",
  extraction_profile_changed: "profil d’extraction différent",
  parser_contract_changed: "contrat d’analyse changé",
  evidence_gate_version_changed: "contrôle de preuve changé",
  archived_output_missing: "sortie archivée introuvable",
  checkpoint_corrupt: "résultat archivé illisible",
  access_mode_incompatible: "mode d’accès incompatible",
};

function planReasonLabel(source: ExtractionProgressSource): string | null {
  const reason = source.plan_reason;
  if (!reason) return null;
  const label = PLAN_REASON_LABELS[reason] ?? reason;
  if (source.plan_disposition === "content_duplicate") {
    return source.plan_primary_source_id
      ? `${label} (${source.plan_primary_source_id})`
      : label;
  }
  return label;
}

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
        {typeof progress.planned_model_calls === "number" ? (
          <span className="extraction-progress__plan">
            Plan : {progress.planned_model_calls} appel
            {progress.planned_model_calls === 1 ? "" : "s"} prévu
            {progress.planned_model_calls === 1 ? "" : "s"} ·{" "}
            {progress.planned_reuses ?? 0} source
            {(progress.planned_reuses ?? 0) === 1 ? "" : "s"} réutilisée
            {(progress.planned_reuses ?? 0) === 1 ? "" : "s"}
          </span>
        ) : null}
      </div>

      <ul
        className="extraction-progress__sources"
        aria-label="Sources de l’extraction"
      >
        {progress.sources.map((source) => {
          const reason = planReasonLabel(source);
          return (
            <li key={source.source_id} className={`is-${source.status}`}>
              <span aria-hidden="true">
                {SOURCE_STATUS_ICONS[source.status]}
              </span>
              <span>{source.source_id}</span>
              <span>{PROFILE_LABELS[source.profile]}</span>
              <span className="extraction-progress__source-status">
                {SOURCE_STATUS_LABELS[source.status]}
                {usesArchiveFallback(source) ? " · Archive de secours" : ""}
                {reason ? ` · ${reason}` : ""}
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
