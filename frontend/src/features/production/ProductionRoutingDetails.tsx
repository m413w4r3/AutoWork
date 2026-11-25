import type {
  ProductionRoutingProjection,
  ProductionRoutingSource,
} from "../../api/production";

function formatLabel(routing: ProductionRoutingProjection | undefined): string {
  if (!routing || routing.status === "not_yet") return "à déterminer";
  if (routing.status === "needs_review") return "Qualification à revoir";
  if (routing.publication_kind === "article") return "Article";
  if (routing.publication_kind === "brief") return "Brève";
  return "à déterminer";
}

function extractionLabel(
  state: ProductionRoutingProjection["extraction_verification_state"],
): string {
  switch (state) {
    case "verified":
      return "Vérifiée";
    case "needs_review":
      return "À vérifier";
    case "stale":
      return "Obsolète";
    case "not_yet":
      return "Pas encore disponible";
  }
}

function sourceLink(sourceUrl: string): { href: string; label: string } {
  try {
    const url = new URL(sourceUrl);
    const label = `${url.host}${url.pathname}`;
    url.username = "";
    url.password = "";
    url.search = "";
    url.hash = "";
    return { href: url.toString(), label };
  } catch {
    const withoutQueryOrFragment = sourceUrl.split(/[?#]/, 1)[0] ?? sourceUrl;
    return {
      href: withoutQueryOrFragment,
      label: withoutQueryOrFragment.replace(/^https?:\/\//i, ""),
    };
  }
}

function collectionStateLabel(state: string | null | undefined): string | null {
  switch (state) {
    case "blocked":
      return "bloquée";
    case "unavailable":
      return "indisponible";
    case "failed_retryable":
      return "en échec, réessayable";
    case "failed_terminal":
      return "en échec définitif";
    case "archived":
      return "archivée";
    case "completed":
    case "succeeded":
      return "collectée";
    case null:
    case undefined:
      return null;
    default:
      return "à vérifier";
  }
}

function responseStatusCode(source: ProductionRoutingSource): string | null {
  const diagnostic = `${source.error_code ?? ""} ${source.failure_code}`;
  return diagnostic.match(/(?:^|\D)(401|403)(?:\D|$)/)?.[1] ?? null;
}

function sourceStateForReason(
  reasonCode: string | null,
  source: ProductionRoutingSource,
): string {
  const collectionState = collectionStateLabel(source.collection_state);
  const sourceDiagnostic =
    `${source.error_code ?? ""} ${source.failure_code}`.toLowerCase();
  if (
    /non_document|not_document|response_page|not_html|non_html/.test(
      sourceDiagnostic,
    )
  ) {
    return collectionState
      ? `${collectionState}; page de réponse non documentaire`
      : "page de réponse non documentaire";
  }
  if (/unreadable/.test(sourceDiagnostic)) {
    return collectionState === "archivée"
      ? "archivée mais illisible"
      : collectionState
        ? `${collectionState}; contenu illisible`
        : "contenu illisible";
  }
  switch (reasonCode) {
    case "critical_source_inaccessible": {
      const statusCode = responseStatusCode(source);
      if (statusCode) return `bloquée par le site distant (${statusCode})`;
      return collectionState
        ? `inaccessible, collecte ${collectionState}`
        : "inaccessible";
    }
    case "critical_source_unreadable":
      return collectionState === "archivée"
        ? "archivée mais illisible"
        : collectionState
          ? `${collectionState}; contenu illisible`
          : "contenu illisible";
    case "critical_source_alternative_provenance":
      return collectionState
        ? `${collectionState}; provenance alternative à vérifier`
        : "provenance alternative à vérifier";
    case "brief_evidence_chronology_missing":
      return collectionState
        ? `${collectionState}, sans date vérifiée`
        : "aucune date vérifiée";
    default:
      return collectionState ?? "état de collecte à vérifier";
  }
}

function sourceRoleAndTier(source: ProductionRoutingSource): string {
  const tier = source.tier ? source.tier.toUpperCase() : "niveau non renseigné";
  const role = source.editorial_role
    ? source.editorial_role.toUpperCase()
    : "rôle non renseigné";
  return `${tier} / ${role}`;
}

function isBlockingSource(source: ProductionRoutingSource): boolean {
  if (source.remaining_control_code === "no_routing_block") return false;
  if (source.remaining_control_code === "human_review_required") return true;
  return source.criticality === "critical";
}

function warningSourceState(source: ProductionRoutingSource): string {
  const diagnostic =
    `${source.error_code ?? ""} ${source.failure_code}`.toLowerCase();
  const collectionState = collectionStateLabel(source.collection_state);
  if (
    /non_document|not_document|response_page|not_html|non_html/.test(diagnostic)
  ) {
    return collectionState
      ? `${collectionState}; page de réponse non documentaire`
      : "page de réponse non documentaire";
  }
  if (/unreadable/.test(diagnostic)) {
    return collectionState === "archivée"
      ? "archivée mais illisible"
      : collectionState
        ? `${collectionState}; contenu illisible`
        : "contenu illisible";
  }
  return collectionState ?? "état de collecte à vérifier";
}

function reviewCause(reasonCode: string | null): string {
  switch (reasonCode) {
    case "critical_source_inaccessible":
      return "Une source critique est inaccessible.";
    case "critical_source_unreadable":
      return "Une source critique ne peut pas être lue.";
    case "critical_source_alternative_provenance":
      return "La provenance alternative d’une source critique doit être vérifiée.";
    case "brief_evidence_chronology_missing":
      return "La chronologie de la brève ne peut pas être établie à partir des événements extraits.";
    case "critical_source_missing":
      return "Source critique manquante ou non lisible (décision antérieure).";
    default:
      return "Le routage nécessite une vérification.";
  }
}

function actionForReason(reasonCode: string | null): string | null {
  switch (reasonCode) {
    case "critical_source_inaccessible":
    case "critical_source_unreadable":
      return "Action disponible : réacquérir ou remplacer la source.";
    case "critical_source_alternative_provenance":
      return "Action disponible : examiner le justificatif ou la preuve d’acquisition.";
    case "brief_evidence_chronology_missing":
      return "Action disponible : une acquisition datée est nécessaire pour les sources CORE/PRIMARY listées.";
    case "critical_source_missing":
      return "Vérifiez la source critique dans les archives et réacquérez-la si nécessaire.";
    default:
      return null;
  }
}

function ProductionRoutingSourceList({
  sources,
  reasonCode,
  warning = false,
}: {
  sources: ProductionRoutingSource[];
  reasonCode: string | null;
  warning?: boolean;
}) {
  return (
    <ul>
      {sources.map((source, index) => {
        const link = sourceLink(source.source_url);
        return (
          <li key={`${source.source_id ?? link.href}-${index}`}>
            <a href={link.href} rel="noreferrer" target="_blank">
              {link.label}
            </a>
            {" — Rôle/tier : "}
            {sourceRoleAndTier(source)}
            {" — État : "}
            {warning
              ? warningSourceState(source)
              : sourceStateForReason(reasonCode, source)}
          </li>
        );
      })}
    </ul>
  );
}

function RoutingReview({ routing }: { routing: ProductionRoutingProjection }) {
  const reasonCode = routing.reason_code;
  const chronologySources = routing.sources.filter(
    (source) => source.failure_code === "brief_chronology_source_undated",
  );
  const blockingSources =
    reasonCode === "brief_evidence_chronology_missing"
      ? chronologySources
      : routing.sources.filter(isBlockingSource);
  const warningSources =
    reasonCode === "brief_evidence_chronology_missing"
      ? []
      : routing.sources.filter((source) => !isBlockingSource(source));
  const action =
    reasonCode === "brief_evidence_chronology_missing" &&
    chronologySources.length === 0
      ? null
      : actionForReason(reasonCode);
  const hasRecomputeGuidance =
    reasonCode === "critical_source_inaccessible" ||
    reasonCode === "critical_source_unreadable" ||
    reasonCode === "critical_source_alternative_provenance" ||
    reasonCode === "brief_evidence_chronology_missing" ||
    reasonCode === "critical_source_missing";

  return (
    <div className="production-routing__review" role="status">
      <p>
        <strong>Routage bloqué</strong> — {reviewCause(reasonCode)}
      </p>
      {reasonCode === "brief_evidence_chronology_missing" ? (
        chronologySources.length > 0 ? (
          <p>
            Aucune date vérifiée n’est disponible pour ces sources CORE/PRIMARY
            :
          </p>
        ) : (
          <>
            <p>
              Les événements extraits ne permettent pas d’établir une
              chronologie datée admissible.
            </p>
            <p>
              Une date vérifiée peut exister, mais l’événement correspondant
              n’est pas admissible pour la brève.
            </p>
            <p>
              Une acquisition de source datée ou une date de publication
              admissible est nécessaire.
            </p>
          </>
        )
      ) : null}
      {blockingSources.length ? (
        <ProductionRoutingSourceList
          sources={blockingSources}
          reasonCode={reasonCode}
        />
      ) : null}
      {action ? <p>{action}</p> : null}
      {hasRecomputeGuidance ? (
        <>
          <p>
            Après l’action, le routage sera recalculé à partir de l’extraction,
            puis les étapes suivantes.
          </p>
          <p>
            Les sources archivées, leurs empreintes et l’historique seront
            conservés.
          </p>
        </>
      ) : null}
      {warningSources.length ? (
        <div>
          <p>
            <strong>Avertissement :</strong> les pertes de sources secondaires
            ou non critiques n’empêchent pas le routage.
          </p>
          <ProductionRoutingSourceList
            sources={warningSources}
            reasonCode={reasonCode}
            warning
          />
        </div>
      ) : null}
    </div>
  );
}

export function ProductionRoutingDetails({
  routing,
}: {
  routing?: ProductionRoutingProjection | null;
}) {
  const confirmedCount = routing?.confirmed_ioc_count;
  return (
    <section className="production-routing" aria-label="Format de publication">
      <p>{`Format : ${formatLabel(routing ?? undefined)}`}</p>
      {routing?.status === "needs_review" ? (
        <RoutingReview routing={routing} />
      ) : null}
      <details>
        <summary>Détail diagnostic</summary>
        <dl>
          <div>
            <dt>Format</dt>
            <dd>{formatLabel(routing ?? undefined)}</dd>
          </div>
          <div>
            <dt>Motif</dt>
            <dd>{routing?.reason_code ?? "Pas encore disponible"}</dd>
          </div>
          <div>
            <dt>Politique</dt>
            <dd>{routing?.policy_version ?? "Pas encore disponible"}</dd>
          </div>
          <div>
            <dt>IOC pertinents :</dt>
            <dd>
              {confirmedCount === null || confirmedCount === undefined
                ? "Pas encore disponible"
                : confirmedCount}
            </dd>
          </div>
          <div>
            <dt>Vérification de l’extraction</dt>
            <dd>
              {extractionLabel(
                routing?.extraction_verification_state ?? "not_yet",
              )}
            </dd>
          </div>
          <div>
            <dt>Sources</dt>
            <dd>
              {routing?.sources.length ? (
                <ul>
                  {routing.sources.map((source, index) => {
                    const link = sourceLink(source.source_url);
                    return (
                      <li key={`${source.source_id ?? link.href}-${index}`}>
                        <a href={link.href} rel="noreferrer" target="_blank">
                          {link.label}
                        </a>
                        <p>
                          {source.source_id
                            ? `Source : ${source.source_id} · `
                            : ""}
                          <code>{source.failure_code}</code>
                          {source.error_code ? (
                            <>
                              {" · "}
                              <code>{source.error_code}</code>
                            </>
                          ) : null}
                          {" · "}
                          {source.tier} / {source.criticality}
                          {` · ${source.criticality_reason}`}
                          {source.editorial_role
                            ? ` / ${source.editorial_role}`
                            : ""}
                          {source.acquisition_method
                            ? ` · ${source.acquisition_method}`
                            : ""}
                          {source.collection_state
                            ? ` · ${source.collection_state}`
                            : ""}
                          {source.remaining_control_code
                            ? ` · ${source.remaining_control_code}`
                            : ""}
                          {source.human_waiver_path
                            ? ` · ${source.human_waiver_path}`
                            : ""}
                          {` · ${source.policy_version}`}
                        </p>
                      </li>
                    );
                  })}
                </ul>
              ) : (
                "Aucun diagnostic de source"
              )}
            </dd>
          </div>
        </dl>
      </details>
    </section>
  );
}
