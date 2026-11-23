import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { InvestigationObservation } from "../../../api/investigation";
import type { ArticleAnchorCatalogViewV1 } from "../api/articleAnchors";
import type {
  NoteContentBlockV1,
  ResolvedNoteContentBlockV1,
} from "../api/opinionProposals";
import {
  createTestQueryClient,
  installFetch,
  jsonResponse,
  wrapperFor,
} from "../api/testSupport";
import { NoteContentBlocks } from "./NoteContentBlocks";

const INVESTIGATION_ID = "11111111-1111-4111-8111-111111111111";
const SUBJECT_ID = "22222222-2222-4222-8222-222222222222";
const OBSERVATION_ID = "33333333-3333-4333-8333-333333333333";
const SECOND_OBSERVATION_ID = "44444444-4444-4444-8444-444444444444";
const ADVISORY_ID = "55555555-5555-4555-8555-555555555555";
const BASE_ID = "66666666-6666-4666-8666-666666666666";
const ELEMENT_HASH = "a".repeat(64);
const OTHER_HASH = "b".repeat(64);
const BLOB_HASH = "c".repeat(64);
const EXCERPT_HASH = "d".repeat(64);
const ASSET_HASH = "e".repeat(64);
const BASE_SHA = "f".repeat(64);

function makeObservation(
  overrides: Partial<InvestigationObservation> = {},
): InvestigationObservation {
  return {
    id: OBSERVATION_ID,
    kind: "binary_analysis",
    sha256: BLOB_HASH,
    source: "analysis-worker",
    source_version: "1",
    observed_at: "2026-11-22T08:00:00Z",
    object_type: "binary",
    object_id: "sample-1",
    limitations: [],
    invocation_ref: null,
    ...overrides,
  };
}

function makeCatalog(): ArticleAnchorCatalogViewV1 {
  return {
    base_publication_artifact_id: BASE_ID,
    base_publication_version: 3,
    base_publication_sha256: BASE_SHA,
    source: "pinned_opinion_base",
    anchors: [
      {
        anchor: "lead:0001",
        text: "Le chapô observe un service de commande.",
        text_sha256: ELEMENT_HASH,
        section_index: null,
        paragraph_index: 0,
      },
    ],
    elements: [
      {
        kind: "table",
        key: "table-commands",
        title: "Commandes observées",
        caption: "Commandes exécutées par l’échantillon.",
        element_sha256: ELEMENT_HASH,
        anchor: "lead:0001",
      },
      {
        kind: "figure",
        key: "figure-c2",
        title: "Topologie C2",
        caption: null,
        element_sha256: OTHER_HASH,
        anchor: "section:2:paragraph:0007",
      },
    ],
    truncated: false,
  };
}

function makeProtocolFigure() {
  return {
    kind: "sequence",
    model_sha256: "1".repeat(64),
    spec_sha256: "2".repeat(64),
    asset_id: "77777777-7777-4777-8777-777777777777",
    asset_sha256: ASSET_HASH,
    preview_sha256: "3".repeat(64),
    pin_sha256: "4".repeat(64),
    compiler_name: "typst",
    compiler_version: "1",
    compiler_policy_version: "1",
    caption: "Séquence des échanges observée.",
  };
}

function installAnchors(catalog = makeCatalog()) {
  return installFetch((request) => {
    if (
      request.url === `/api/investigations/${INVESTIGATION_ID}/article-anchors`
    ) {
      return jsonResponse(catalog);
    }
    return jsonResponse({ detail: { code: "unexpected_request" } }, 500);
  });
}

function renderBlocks(
  props: Partial<Parameters<typeof NoteContentBlocks>[0]> = {},
) {
  const client = createTestQueryClient();
  return render(
    <NoteContentBlocks
      investigationId={INVESTIGATION_ID}
      anchor="lead:0001"
      observations={[makeObservation()]}
      protocolFigures={[makeProtocolFigure()]}
      blocks={[]}
      subjectId={SUBJECT_ID}
      {...props}
    />,
    { wrapper: wrapperFor(client) },
  );
}

function SelectionHarness({
  onChange,
  observations,
  protocolFigures,
}: {
  onChange: (blocks: NoteContentBlockV1[]) => void;
  observations: InvestigationObservation[];
  protocolFigures?: ReturnType<typeof makeProtocolFigure>[];
}) {
  const [blocks, setBlocks] = useState<NoteContentBlockV1[]>([]);
  return (
    <NoteContentBlocks
      investigationId={INVESTIGATION_ID}
      anchor="lead:0001"
      observations={observations}
      protocolFigures={protocolFigures ?? [makeProtocolFigure()]}
      blocks={blocks}
      onChange={(next) => {
        setBlocks(next);
        onChange(next);
      }}
      subjectId={SUBJECT_ID}
    />
  );
}

function renderSelection(
  observations: InvestigationObservation[] = [makeObservation()],
) {
  const onChange = vi.fn<(blocks: NoteContentBlockV1[]) => void>();
  const client = createTestQueryClient();
  const result = render(
    <SelectionHarness onChange={onChange} observations={observations} />,
    { wrapper: wrapperFor(client) },
  );
  return { ...result, onChange };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("NoteContentBlocks — sélection de références", () => {
  it("ne propose que les éléments M23 rendus à l’ancre et les référence par valeur", async () => {
    const user = userEvent.setup();
    installAnchors();
    const { onChange } = renderSelection();

    const elementSelect = await screen.findByLabelText("Élément à référencer");
    const options = within(elementSelect).getAllByRole("option");
    expect(
      options.map((option) => option.textContent ?? "").join(" | "),
    ).toContain("Commandes observées");
    expect(
      options.map((option) => option.textContent ?? "").join(" | "),
    ).not.toContain("Topologie C2");

    await user.selectOptions(elementSelect, "table:table-commands");
    await user.click(
      screen.getByRole("button", { name: "Référencer cet élément" }),
    );

    expect(onChange).toHaveBeenLastCalledWith([
      {
        block_type: "article_element",
        kind: "table",
        element_key: "table-commands",
        element_sha256: ELEMENT_HASH,
        anchor: "lead:0001",
      },
    ]);
    expect(screen.getByText(/Élément d’article/)).toBeInTheDocument();
    expect(screen.getByText(/table-commands/)).toBeInTheDocument();
  });

  it("dérive le titre du tableau IOC et exclut les observations consultatives", async () => {
    const user = userEvent.setup();
    installAnchors();
    const advisory = makeObservation({
      id: ADVISORY_ID,
      source: "advisor.consult",
      object_type: "advisor_call",
      object_id: "advisor-1",
    });
    const second = makeObservation({
      id: SECOND_OBSERVATION_ID,
      object_id: "sample-2",
      sha256: OTHER_HASH,
    });
    const { onChange } = renderSelection([makeObservation(), second, advisory]);

    const ioc = await screen.findByRole("group", {
      name: /Tableau IOC depuis les observations canoniques/,
    });
    expect(within(ioc).queryByRole("checkbox", { name: /advisor/ })).toBeNull();
    expect(within(ioc).getAllByRole("checkbox")).toHaveLength(2);

    await user.click(within(ioc).getByRole("checkbox", { name: /sample-1/ }));
    await user.click(within(ioc).getByRole("checkbox", { name: /sample-2/ }));
    await user.click(
      within(ioc).getByRole("button", { name: "Ajouter le tableau IOC (2)" }),
    );

    expect(onChange).toHaveBeenLastCalledWith([
      {
        block_type: "ioc_table",
        observation_ids: [OBSERVATION_ID, SECOND_OBSERVATION_ID],
        title: "Tableau IOC (2)",
      },
    ]);
  });

  it("référence un extrait de code exact et refuse un SHA-256 invalide", async () => {
    const user = userEvent.setup();
    installAnchors();
    const { onChange } = renderSelection();

    await user.selectOptions(
      await screen.findByLabelText("Observation source"),
      OBSERVATION_ID,
    );
    await user.type(screen.getByLabelText("Offset (octets)"), "12");
    await user.type(screen.getByLabelText("Longueur (octets)"), "42");
    await user.clear(screen.getByLabelText("Langage"));
    await user.type(screen.getByLabelText("Langage"), "python");
    await user.type(screen.getByLabelText("SHA-256 de l’extrait"), "abc");

    const add = screen.getByRole("button", {
      name: "Référencer cet extrait de code",
    });
    expect(add).toBeDisabled();
    expect(
      screen.getByText(/SHA-256 de l’extrait doit être 64 caractères/),
    ).toBeInTheDocument();

    await user.clear(screen.getByLabelText("SHA-256 de l’extrait"));
    await user.type(
      screen.getByLabelText("SHA-256 de l’extrait"),
      EXCERPT_HASH,
    );
    expect(add).toBeEnabled();
    await user.click(add);

    expect(onChange).toHaveBeenLastCalledWith([
      {
        block_type: "canonical_code",
        observation_id: OBSERVATION_ID,
        blob_sha256: BLOB_HASH,
        offset: 12,
        length: 42,
        excerpt_sha256: EXCERPT_HASH,
        language: "python",
      },
    ]);
  });

  it("référence une figure de protocole déjà portée par l’avis", async () => {
    const user = userEvent.setup();
    installAnchors();
    const { onChange } = renderSelection();

    await user.selectOptions(
      await screen.findByLabelText("Figure à référencer"),
      "sequence",
    );
    await user.click(
      screen.getByRole("button", { name: "Référencer cette figure" }),
    );

    expect(onChange).toHaveBeenLastCalledWith([
      {
        block_type: "protocol_figure",
        kind: "sequence",
        asset_sha256: ASSET_HASH,
      },
    ]);
  });

  it("n’offre aucune saisie de prose publiable", () => {
    installAnchors();
    renderSelection();

    expect(
      screen.queryByRole("textbox", {
        name: /(texte|proposition|légende).*(note|bloc|avis)/i,
      }),
    ).toBeNull();
    expect(screen.queryByRole("textbox", { name: /légende/i })).toBeNull();
  });
});

describe("NoteContentBlocks — aperçu lecture seule", () => {
  const blocks: NoteContentBlockV1[] = [
    {
      block_type: "article_element",
      kind: "table",
      element_key: "table-commands",
      element_sha256: ELEMENT_HASH,
      anchor: "lead:0001",
    },
    {
      block_type: "canonical_code",
      observation_id: OBSERVATION_ID,
      blob_sha256: BLOB_HASH,
      offset: 12,
      length: 14,
      excerpt_sha256: EXCERPT_HASH,
      language: "bash",
    },
    {
      block_type: "ioc_table",
      observation_ids: [OBSERVATION_ID],
      title: "Tableau IOC (1)",
    },
    {
      block_type: "protocol_figure",
      kind: "sequence",
      asset_sha256: ASSET_HASH,
    },
  ];

  const resolved: ResolvedNoteContentBlockV1[] = [
    {
      ...(blocks[0] as Extract<
        NoteContentBlockV1,
        { block_type: "article_element" }
      >),
      title: "Commandes observées",
      caption: "Commandes exécutées par l’échantillon.",
    },
    {
      ...(blocks[1] as Extract<
        NoteContentBlockV1,
        { block_type: "canonical_code" }
      >),
      excerpt: "cmd.exe /c whoami",
      observation_source: "analysis-worker",
      observation_object_type: "binary",
      observation_object_id: "sample-1",
    },
    {
      ...(blocks[2] as Extract<
        NoteContentBlockV1,
        { block_type: "ioc_table" }
      >),
      rows: [
        {
          observation_id: OBSERVATION_ID,
          object_type: "binary",
          object_id: "sample-1",
          source: "analysis-worker",
          observed_at: "2026-11-22T08:00:00Z",
          blob_sha256: BLOB_HASH,
        },
      ],
    },
    {
      ...(blocks[3] as Extract<
        NoteContentBlockV1,
        { block_type: "protocol_figure" }
      >),
      caption: "Séquence des échanges observée.",
    },
  ];

  it("rend les cross-références, le code, le tableau IOC et la figure avec provenance", () => {
    renderBlocks({
      observations: [],
      protocolFigures: [],
      blocks,
      resolvedBlocks: resolved,
      basePublication: {
        artifactId: BASE_ID,
        version: 3,
        sha256: BASE_SHA,
      },
    });

    expect(
      screen.getByRole("heading", {
        name: "Aperçu lecture seule des blocs vérifiés",
      }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Commandes observées/)).toBeInTheDocument();
    expect(
      screen.getByText(/cross-référence vers l’ancre/),
    ).toBeInTheDocument();
    expect(screen.getByText("cmd.exe /c whoami")).toBeInTheDocument();
    expect(
      screen.getByRole("table", { name: "Tableau IOC (1)" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Séquence des échanges observée/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Base de publication épinglée : artefact/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", {
        name: "Ouvrir dans l’atelier éditorial",
      }),
    ).toHaveAttribute("href", `/subjects/${SUBJECT_ID}/editor`);
  });

  it("traduit un code de référence périmée et recharge la proposition", async () => {
    const user = userEvent.setup();
    const onReload = vi.fn();
    renderBlocks({
      observations: [],
      blocks,
      resolvedBlocks: [],
      contentBlockError: "note_block_ref_stale",
      onReload,
    });

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(/Un élément d’article référencé a changé/);
    expect(alert).toHaveTextContent(/code : note_block_ref_stale/);
    await user.click(
      screen.getByRole("button", { name: "Recharger la proposition" }),
    );
    expect(onReload).toHaveBeenCalledTimes(1);
  });
});
