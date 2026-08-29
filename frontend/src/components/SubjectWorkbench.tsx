import { useState } from "react";

import { ArticleTab } from "../features/subject/ArticleTab";
import { AssetsTab } from "../features/subject/AssetsTab";
import { IndicatorsTab } from "../features/subject/IndicatorsTab";
import { PipelineTab } from "../features/subject/PipelineTab";
import { Link } from "../routing";

type SubjectTab = "article" | "indicators" | "assets" | "pipeline";

const TABS: ReadonlyArray<readonly [SubjectTab, string]> = [
  ["article", "Article"],
  ["indicators", "IOC"],
  ["assets", "Sources et fichiers"],
  ["pipeline", "Pipeline"],
];

export function SubjectWorkbench({ subjectId }: { subjectId: string }) {
  const [tab, setTab] = useState<SubjectTab>("article");

  return (
    <section className="subject-workbench">
      <Link to="/editions">← Retour aux éditions</Link>
      <div className="detail-heading">
        <div>
          <p className="eyebrow">Sujet</p>
          <h1>Article</h1>
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
