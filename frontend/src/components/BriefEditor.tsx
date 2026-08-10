import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import {
  approveBrief,
  briefMarkdownUrl,
  editBriefBlock,
  freezeBriefPack,
  generateBrief,
  getBrief,
  promoteBrief,
  regenerateBriefBlock,
  requestBriefChanges,
  type BriefBlock,
} from "../api/briefs";
import { JobStatusCard } from "./JobStatusCard";

export function BriefEditor({ subjectId }: { subjectId: string }) {
  const queryClient = useQueryClient();
  const [provider, setProvider] = useState<"qwen" | "openai">("qwen");
  const [jobId, setJobId] = useState<string | null>(null);
  const brief = useQuery({
    queryKey: ["brief", subjectId],
    queryFn: () => getBrief(subjectId),
    refetchInterval: jobId ? 2_000 : false,
  });
  const action = useMutation({
    mutationFn: (operation: () => Promise<unknown>) => operation(),
    onSuccess: (result) => {
      if (
        typeof result === "object" &&
        result !== null &&
        "job_id" in result &&
        typeof result.job_id === "string"
      ) {
        setJobId(result.job_id);
      }
      void queryClient.invalidateQueries({ queryKey: ["brief", subjectId] });
    },
  });

  if (brief.isPending) return <p role="status">Chargement de la brève…</p>;
  if (brief.isError)
    return (
      <p role="alert" className="error-message">
        La brève est inaccessible.
      </p>
    );
  const data = brief.data;
  return (
    <section className="brief-editor" aria-label="Éditeur de brève">
      <div className="brief-toolbar">
        <div>
          <h2>Brève publiable</h2>
          <p>
            {data.pack
              ? `Pack gelé v${data.pack.version} · ${data.pack.claim_count} claims · ${data.pack.indicator_count} IOC`
              : "Aucun pack de preuves gelé"}
          </p>
        </div>
        <label>
          Modèle
          <select
            value={provider}
            onChange={(event) =>
              setProvider(event.target.value as "qwen" | "openai")
            }
          >
            <option value="qwen">Qwen local (défaut)</option>
            <option value="openai">OpenAI via bridge</option>
          </select>
        </label>
        <button
          className="button button--secondary"
          onClick={() => action.mutate(() => freezeBriefPack(subjectId))}
        >
          Geler les preuves
        </button>
        <button
          className="button"
          disabled={action.isPending}
          onClick={() =>
            action.mutate(() => generateBrief(subjectId, provider))
          }
        >
          {data.draft ? "Régénérer la brève" : "Générer la brève"}
        </button>
      </div>
      {action.error ? (
        <p role="alert" className="error-message">
          {action.error.message}
        </p>
      ) : null}
      {jobId ? (
        <JobStatusCard
          jobId={jobId}
          onTerminal={() => {
            setJobId(null);
            void queryClient.invalidateQueries({
              queryKey: ["brief", subjectId],
            });
          }}
        />
      ) : null}
      {data.status === "stale" ? (
        <p role="alert" className="verification-warning">
          Les preuves ont changé : ce brouillon est invalidé. Régénérez-le
          depuis le pack courant.
        </p>
      ) : null}
      {data.draft ? (
        <>
          <div className="badge-row">
            <span className="status-badge">{data.status}</span>
            <span>
              Version {data.draft.version} · {data.draft.provider}
            </span>
          </div>
          <h2>{data.draft.title}</h2>
          <div className="brief-blocks">
            {data.blocks.map((block, index) => (
              <EditableBlock
                key={`${block.id}-${data.draft?.version}`}
                block={block}
                index={index}
                pending={action.isPending}
                onSave={(texts) =>
                  action.mutate(() =>
                    editBriefBlock(subjectId, block.id, texts),
                  )
                }
                onRegenerate={() =>
                  action.mutate(() =>
                    regenerateBriefBlock(subjectId, block.id, provider),
                  )
                }
              />
            ))}
          </div>
          <section
            className="qa-panel"
            aria-label="Contrôles avant approbation"
          >
            <h3>Contrôles automatiques</h3>
            <ul>
              {Object.entries(data.qa).map(([name, passed]) => (
                <li key={name}>
                  {passed ? "✓" : "✗"} {name.replaceAll("_", " ")}
                </li>
              ))}
            </ul>
            {data.qa_errors.map((error) => (
              <p className="error-message" key={error}>
                {error}
              </p>
            ))}
          </section>
          <div className="editorial-actions">
            <button
              className="button button--secondary"
              onClick={() => {
                const note = window.prompt("Modification demandée");
                if (note)
                  action.mutate(() => requestBriefChanges(subjectId, note));
              }}
            >
              Demander une modification
            </button>
            <button
              className="button"
              disabled={
                data.status === "stale" ||
                Object.values(data.qa).some((value) => !value)
              }
              onClick={() => action.mutate(() => approveBrief(subjectId))}
            >
              Approuver
            </button>
            <button
              className="button button--secondary"
              disabled={data.status !== "approved"}
              onClick={() => action.mutate(() => promoteBrief(subjectId))}
            >
              Promouvoir en article principal
            </button>
            {data.status === "approved" || data.status === "promoted" ? (
              <a
                className="button button--secondary"
                href={briefMarkdownUrl(subjectId)}
              >
                Exporter Markdown
              </a>
            ) : null}
          </div>
          {data.diff ? (
            <details>
              <summary>Diff avec la version précédente</summary>
              <pre className="brief-diff">{data.diff}</pre>
            </details>
          ) : null}
        </>
      ) : (
        <p className="empty-state">
          Gelez les preuves validées puis générez la première version.
        </p>
      )}
    </section>
  );
}

function EditableBlock({
  block,
  index,
  pending,
  onSave,
  onRegenerate,
}: {
  block: BriefBlock;
  index: number;
  pending: boolean;
  onSave: (texts: string[]) => void;
  onRegenerate: () => void;
}) {
  const [texts, setTexts] = useState(block.sentences.map((item) => item.text));
  useEffect(() => setTexts(block.sentences.map((item) => item.text)), [block]);
  return (
    <article className="brief-block">
      <div className="brief-block__heading">
        <h3>Paragraphe {index + 1}</h3>
        <div>
          <button
            className="button button--secondary"
            disabled={pending}
            onClick={() => onSave(texts)}
          >
            Enregistrer le bloc
          </button>
          <button
            className="button button--secondary"
            disabled={pending}
            onClick={onRegenerate}
          >
            Régénérer ce bloc
          </button>
        </div>
      </div>
      {block.sentences.map((sentence, sentenceIndex) => (
        <div className="brief-sentence" key={sentence.id}>
          <textarea
            aria-label={`Phrase ${sentenceIndex + 1} du paragraphe ${index + 1}`}
            value={texts[sentenceIndex]}
            onChange={(event) =>
              setTexts((current) =>
                current.map((text, itemIndex) =>
                  itemIndex === sentenceIndex ? event.target.value : text,
                ),
              )
            }
          />
          <aside aria-label={`Preuves de la phrase ${sentenceIndex + 1}`}>
            <strong>
              {sentence.factual ? "Phrase factuelle" : "Phrase éditoriale"}
            </strong>
            {sentence.evidence.map((claim) => (
              <p key={claim.id}>
                <code>{claim.id}</code>
                <br />
                {claim.value}
              </p>
            ))}
          </aside>
        </div>
      ))}
    </article>
  );
}
