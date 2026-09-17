import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { getSubject } from "../api/editions";
import { TlpBadge } from "../features/editions/editionPresentation";
import { ArticleTab } from "../features/subject/ArticleTab";
import { AssetsTab } from "../features/subject/AssetsTab";
import { IndicatorsTab } from "../features/subject/IndicatorsTab";
import { PipelineTab } from "../features/subject/PipelineTab";
import { Link } from "../routing";

type SubjectTab = "article" | "indicators" | "assets" | "pipeline";
const REJECTION_ANCHOR = "production-rejections-heading";

const TABS: ReadonlyArray<readonly [SubjectTab, string]> = [
  ["article", "Article"],
  ["indicators", "IOC"],
  ["assets", "Sources et fichiers"],
  ["pipeline", "Pipeline"],
];

export function SubjectWorkbench({ subjectId }: { subjectId: string }) {
  const [tab, setTab] = useState<SubjectTab>(() =>
    window.location.hash === "#" + REJECTION_ANCHOR ? "pipeline" : "article",
  );
  const subjectQuery = useQuery({
    queryKey: ["subject", subjectId],
    queryFn: () => getSubject(subjectId),
  });

  if (subjectQuery.isPending) {
    return (
      <section className="subject-workbench" aria-busy="true">
        <p role="status">Chargement du sujet…</p>
      </section>
    );
  }

  if (subjectQuery.isError || !subjectQuery.data) {
    return (
      <section className="subject-workbench">
        <p role="alert">Impossible de charger le sujet.</p>
      </section>
    );
  }

  const subject = subjectQuery.data;

  return (
    <section className="subject-workbench">
      <Link to={`/editions/${subject.edition_id}`}>← Retour à l’édition</Link>
      <div className="detail-heading">
        <div>
          <p className="eyebrow">Sujet</p>
          <h1>{subject.title}</h1>
          <TlpBadge tlp={subject.tlp} />
        </div>
      </div>

      <nav className="workbench-tabs" aria-label="Contenu du sujet">
        {TABS.map(([value, label]) => (
          <button
            key={value}
            type="button"
            aria-pressed={tab === value}
            onClick={() => setTab(value)}
          >
            {label}
          </button>
        ))}
      </nav>

      {tab === "article" ? (
        <ArticleTab
          subjectId={subjectId}
          onOpenPipeline={() => setTab("pipeline")}
        />
      ) : null}
      {tab === "indicators" ? <IndicatorsTab subjectId={subjectId} /> : null}
      {tab === "assets" ? <AssetsTab subjectId={subjectId} /> : null}
      {tab === "pipeline" ? <PipelineTab subjectId={subjectId} /> : null}
    </section>
  );
}
