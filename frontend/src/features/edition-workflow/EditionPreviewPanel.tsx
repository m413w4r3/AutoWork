import { useQuery } from "@tanstack/react-query";
import { useRef, useState } from "react";

import {
  editionPreviewDocxUrl,
  getEditionPreview,
} from "../../api/publication";

export function EditionPreviewPanel({ editionId }: { editionId: string }) {
  const previousHash = useRef<string | null>(null);
  const [view, setView] = useState<"bulletin" | "markdown">("bulletin");
  const preview = useQuery({
    queryKey: ["edition-preview", editionId],
    queryFn: async () => {
      const result = await getEditionPreview(editionId, previousHash.current);
      previousHash.current = result.preview_input_hash;
      return result;
    },
    retry: false,
  });

  if (preview.isPending)
    return <p role="status">Chargement de la prévisualisation…</p>;
  if (preview.isError) {
    return (
      <p className="error-message" role="alert">
        La prévisualisation est inaccessible : {String(preview.error)}
      </p>
    );
  }
  if (!preview.data) return null;

  const current = preview.data;
  return (
    <section
      className="edition-preview"
      aria-labelledby="edition-preview-heading"
    >
      <div className="edition-preview__heading">
        <div>
          <p className="eyebrow">Prévisualisation</p>
          <h2 id="edition-preview-heading">Vue bulletin</h2>
        </div>
        <button
          className="button button--secondary"
          type="button"
          onClick={() => void preview.refetch()}
        >
          Actualiser
        </button>
      </div>
      {current.stale ? (
        <p className="verification-warning" role="alert">
          Cette prévisualisation est obsolète : l’édition ou un article a
          changé. Actualisez-la avant téléchargement.
        </p>
      ) : null}
      <nav className="workbench-tabs" aria-label="Format de prévisualisation">
        <button
          type="button"
          aria-pressed={view === "bulletin"}
          onClick={() => setView("bulletin")}
        >
          Vue bulletin
        </button>
        <button
          type="button"
          aria-pressed={view === "markdown"}
          onClick={() => setView("markdown")}
        >
          Markdown canonique
        </button>
      </nav>
      {view === "bulletin" ? (
        <iframe
          className="edition-preview__frame"
          title="Vue bulletin"
          sandbox=""
          srcDoc={current.sanitized_html}
        />
      ) : (
        <pre className="edition-preview__markdown">
          {current.canonical_markdown}
        </pre>
      )}
      <p>
        {current.stale ? (
          "Téléchargement désactivé tant que la prévisualisation est obsolète."
        ) : (
          <a
            className="button"
            href={editionPreviewDocxUrl(editionId, current.preview_input_hash)}
            download
          >
            Télécharger DOCX de prévisualisation
          </a>
        )}
      </p>
      <p className="edition-preview__provenance">
        Hash d’entrée : <code>{current.preview_input_hash.slice(0, 12)}…</code>{" "}
        · {current.artifacts.length} article
        {current.artifacts.length > 1 ? "s" : ""}
      </p>
    </section>
  );
}
