import { useQuery } from "@tanstack/react-query";

import {
  getSubjectAssets,
  type SubjectAssetResponse,
} from "../../api/subjectContent";

export function AssetsTab({ subjectId }: { subjectId: string }) {
  const assets = useQuery({
    queryKey: ["subject-assets", subjectId],
    queryFn: () => getSubjectAssets(subjectId),
  });

  if (assets.isPending)
    return <p role="status">Chargement des sources et fichiers…</p>;
  if (assets.isError) {
    return (
      <p className="error-message" role="alert">
        Les sources et fichiers sont inaccessibles : {String(assets.error)}
      </p>
    );
  }
  if (!assets.data)
    return <p className="empty-state">Aucune source ni fichier disponible.</p>;

  return (
    <section className="subject-assets">
      <AssetSection title="Sources" values={assets.data.sources} />
      <AssetSection title="Fichiers" values={assets.data.samples} />
    </section>
  );
}

function AssetSection({
  title,
  values,
}: {
  title: string;
  values: SubjectAssetResponse[];
}) {
  return (
    <section aria-labelledby={`${title.toLowerCase()}-heading`}>
      <h2 id={`${title.toLowerCase()}-heading`}>{title}</h2>
      {values.length === 0 ? (
        <p className="empty-state">Aucun élément.</p>
      ) : (
        <ul className="subject-asset-list">
          {values.map((value) => (
            <li key={value.id}>
              <strong>{value.original_name}</strong>
              <dl className="edition-facts">
                <div>
                  <dt>Origine</dt>
                  <dd>{value.origin}</dd>
                </div>
                {value.mime_type ? (
                  <div>
                    <dt>Type</dt>
                    <dd>{value.mime_type}</dd>
                  </div>
                ) : null}
                {value.sha256 ? (
                  <div>
                    <dt>SHA-256</dt>
                    <dd>
                      <code>{value.sha256}</code>
                    </dd>
                  </div>
                ) : null}
                {value.size !== null ? (
                  <div>
                    <dt>Taille</dt>
                    <dd>{value.size} octets</dd>
                  </div>
                ) : null}
                {value.provenance ? (
                  <div>
                    <dt>Provenance</dt>
                    <dd>
                      {Object.entries(value.provenance)
                        .map(([key, item]) => `${key}: ${item}`)
                        .join(" · ")}
                    </dd>
                  </div>
                ) : null}
              </dl>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
