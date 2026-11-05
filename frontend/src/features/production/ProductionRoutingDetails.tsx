import type { ProductionRoutingProjection } from "../../api/production";

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

export function ProductionRoutingDetails({
  routing,
}: {
  routing?: ProductionRoutingProjection | null;
}) {
  const confirmedCount = routing?.confirmed_ioc_count;
  return (
    <section className="production-routing" aria-label="Format de publication">
      <p>
        <strong>Format :</strong> {formatLabel(routing ?? undefined)}
      </p>
      <details>
        <summary>Détails du format</summary>
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
                  {routing.sources.map((source, index) => (
                    <li
                      key={`${source.source_id ?? source.source_url}-${index}`}
                    >
                      <a
                        href={source.source_url}
                        rel="noreferrer"
                        target="_blank"
                      >
                        {source.source_url}
                      </a>
                      {" · "}
                      {source.failure_code}
                    </li>
                  ))}
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
