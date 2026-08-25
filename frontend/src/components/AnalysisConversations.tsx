import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import {
  addConversationTurn,
  archiveModelConversation,
  createModelConversation,
  listConversationTurns,
  listModelConversations,
  type ConversationPurpose,
  type ModelConversation,
} from "../api/modelConversations";

export function AnalysisConversations({ subjectId }: { subjectId: string }) {
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [purpose, setPurpose] =
    useState<ConversationPurpose>("analyst_assistance");
  const [provider, setProvider] = useState<"openai" | "qwen">("openai");
  const [profile, setProfile] = useState("");
  const [model, setModel] = useState("");
  const [message, setMessage] = useState("");
  const [externalAllowed, setExternalAllowed] = useState(false);
  const conversations = useQuery({
    queryKey: ["model-conversations", subjectId],
    queryFn: () => listModelConversations(subjectId),
  });
  const selected =
    conversations.data?.find((item) => item.id === selectedId) ?? null;
  const canContinue = Boolean(
    selected &&
    selected.turn_count > 0 &&
    ["analyst_assistance", "pivot_research"].includes(selected.purpose) &&
    selected.transport !== "application_managed",
  );
  const turns = useQuery({
    queryKey: ["model-conversation-turns", subjectId, selectedId],
    queryFn: () => listConversationTurns(selectedId!, subjectId),
    enabled: Boolean(selectedId),
  });
  const refresh = async (conversationId?: string) => {
    await queryClient.invalidateQueries({
      queryKey: ["model-conversations", subjectId],
    });
    if (conversationId) {
      await queryClient.invalidateQueries({
        queryKey: ["model-conversation-turns", subjectId, conversationId],
      });
    }
  };
  const create = useMutation({
    mutationFn: () =>
      createModelConversation({
        subject_id: subjectId,
        title,
        purpose,
        provider,
        expected_profile: profile || null,
        requested_model: model || null,
      }),
    onSuccess: async (conversation) => {
      setSelectedId(conversation.id);
      setTitle("");
      await refresh();
    },
  });
  const send = useMutation({
    mutationFn: ({
      conversation,
      mode,
    }: {
      conversation: ModelConversation;
      mode: "fresh" | "continue";
    }) =>
      addConversationTurn(conversation.id, subjectId, {
        message,
        mode,
        external_llm_allowed: externalAllowed,
        idempotency_key: crypto.randomUUID(),
      }),
    onSuccess: async (_, variables) => {
      setMessage("");
      await refresh(variables.conversation.id);
    },
  });
  const archive = useMutation({
    mutationFn: (conversation: ModelConversation) =>
      archiveModelConversation(conversation.id, subjectId),
    onSuccess: async () => refresh(selectedId || undefined),
  });
  const error = create.error || send.error || archive.error;

  return (
    <section
      className="conversation-panel"
      aria-labelledby="conversation-heading"
    >
      <header>
        <div>
          <p className="eyebrow">Assistance analyste</p>
          <h2 id="conversation-heading">Conversations d’analyse</h2>
        </div>
        <p className="verification-warning" role="note">
          Ces échanges ne sont ni des preuves primaires, ni des claims ou IOC
          validés, et n’entrent jamais automatiquement dans le pack de preuves.
        </p>
      </header>

      <form
        className="conversation-form"
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate();
        }}
      >
        <label>
          Titre défini par l’application
          <input
            required
            maxLength={500}
            value={title}
            onChange={(event) => setTitle(event.target.value)}
          />
        </label>
        <label>
          Objectif
          <select
            value={purpose}
            onChange={(event) =>
              setPurpose(event.target.value as ConversationPurpose)
            }
          >
            <option value="analyst_assistance">Assistance analyste</option>
            <option value="pivot_research">Recherche de pivots</option>
            <option value="subject_research">Recherche production</option>
            <option value="discovery">Découverte (fresh uniquement)</option>
            <option value="drafting">Rédaction</option>
            <option value="critic">Critique (fresh uniquement)</option>
          </select>
        </label>
        <label>
          Provider
          <select
            value={provider}
            onChange={(event) =>
              setProvider(event.target.value as "openai" | "qwen")
            }
          >
            <option value="openai">OpenAI via ChatGPT bridge</option>
            <option value="qwen">Qwen local</option>
          </select>
        </label>
        <label>
          Profil attendu
          <input
            value={profile}
            onChange={(event) => setProfile(event.target.value)}
          />
        </label>
        <label>
          Modèle / profil applicatif
          <input
            value={model}
            onChange={(event) => setModel(event.target.value)}
          />
        </label>
        <button className="button" disabled={create.isPending || !title.trim()}>
          Nouvelle conversation
        </button>
      </form>

      {error ? (
        <p className="error-message" role="alert">
          {error.message}
        </p>
      ) : null}
      <div className="conversation-layout">
        <div className="conversation-list" aria-label="Liste des conversations">
          {conversations.data?.length ? (
            conversations.data.map((conversation) => (
              <button
                key={conversation.id}
                className="conversation-list__item"
                aria-pressed={conversation.id === selectedId}
                onClick={() => setSelectedId(conversation.id)}
              >
                <strong>{conversation.title}</strong>
                <span>
                  {conversation.status} · {conversation.turn_count} tour(s)
                </span>
                <span>
                  {conversation.last_used_at
                    ? new Date(conversation.last_used_at).toLocaleString(
                        "fr-FR",
                      )
                    : "Jamais utilisée"}
                </span>
              </button>
            ))
          ) : (
            <p className="empty-state">Aucune conversation pour ce sujet.</p>
          )}
        </div>

        {selected ? (
          <div className="conversation-detail">
            <div className="editorial-actions">
              <h3>{selected.title}</h3>
              <button
                className="button button--danger"
                disabled={archive.isPending || selected.status === "archived"}
                onClick={() => archive.mutate(selected)}
              >
                Archiver
              </button>
            </div>
            {selected.status === "needs_review" ||
            selected.status === "unavailable" ? (
              <p className="error-message" role="alert">
                Conversation {selected.status} : réconciliation requise avant
                toute continuation.
              </p>
            ) : null}
            <ol className="conversation-timeline">
              {turns.data?.map((turn) => (
                <li key={turn.id}>
                  <p>
                    <strong>Q{turn.sequence}</strong> {turn.input_text}
                  </p>
                  <p>
                    <strong>R{turn.sequence}</strong>{" "}
                    {turn.output_text || turn.error?.message || turn.status}
                  </p>
                  <small>
                    ModelRun {turn.model_run_id} · correlation_id{" "}
                    {turn.correlation_id}
                  </small>
                </li>
              ))}
            </ol>
            {selected.status !== "archived" ? (
              <div className="conversation-compose">
                <label>
                  Question
                  <textarea
                    value={message}
                    onChange={(event) => setMessage(event.target.value)}
                  />
                </label>
                <label className="conversation-policy-check">
                  <input
                    type="checkbox"
                    checked={externalAllowed}
                    onChange={(event) =>
                      setExternalAllowed(event.target.checked)
                    }
                  />
                  La classification et la politique de diffusion autorisent ce
                  message vers un LLM externe
                </label>
                <button
                  className="button"
                  disabled={
                    send.isPending ||
                    !message.trim() ||
                    (selected.provider === "openai" && !externalAllowed) ||
                    ["busy", "needs_review", "unavailable"].includes(
                      selected.status,
                    )
                  }
                  onClick={() =>
                    send.mutate({
                      conversation: selected,
                      mode: canContinue ? "continue" : "fresh",
                    })
                  }
                >
                  {canContinue
                    ? "Continuer cette conversation"
                    : selected.turn_count
                      ? "Envoyer une question isolée (fresh)"
                      : "Envoyer le premier message"}
                </button>
              </div>
            ) : null}
          </div>
        ) : null}
      </div>
    </section>
  );
}
