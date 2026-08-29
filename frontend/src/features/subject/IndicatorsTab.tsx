import { useQuery } from "@tanstack/react-query";

import {
  getSubjectIndicators,
  type SubjectIndicatorResponse,
} from "../../api/subjectContent";

export function IndicatorsTab({ subjectId }: { subjectId: string }) {
  const indicators = useQuery({
    queryKey: ["subject-indicators", subjectId],
    queryFn: () => getSubjectIndicators(subjectId),
  });

  if (indicators.isPending) return <p role="status">Chargement des IOC…</p>;
  if (indicators.isError) {
    return (
      <p className="error-message" role="alert">
        Les IOC sont inaccessibles : {String(indicators.error)}
      </p>
    );
  }
  if (indicators.data.length === 0) {
    return <p className="empty-state">Aucun IOC vérifié pour cet article.</p>;
  }

  return (
    <section aria-labelledby="indicators-heading">
      <h2 id="indicators-heading">IOC</h2>
      <div className="subject-data-table" role="table">
        <div
          className="subject-data-table__row subject-data-table__row--header"
          role="row"
        >
          <strong role="columnheader">Type</strong>
          <strong role="columnheader">Valeur</strong>
          <strong role="columnheader">Statut</strong>
          <strong role="columnheader">Sources</strong>
        </div>
        {indicators.data.map((indicator) => (
          <IndicatorRow key={indicator.id} indicator={indicator} />
        ))}
      </div>
    </section>
  );
}

function IndicatorRow({ indicator }: { indicator: SubjectIndicatorResponse }) {
  return (
    <div className="subject-data-table__row" role="row">
      <span role="cell">{indicator.artifact_type}</span>
      <code role="cell">{indicator.display_value}</code>
      <span role="cell">{indicator.indicator_status}</span>
      <span role="cell">{indicator.source_ids.join(", ") || "—"}</span>
    </div>
  );
}
