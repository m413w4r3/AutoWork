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
  errorResponse,
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
const CHART_HASH = "0".repeat(64);
const BLOB_HASH = "c".repeat(64);
const IOC_OBJECT_ID = "0123456789abcdef".repeat(4);
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
        proposable: true,
        refusal_reason: null,
      },
      {
        kind: "chart",
        key: "chart-volume",
        title: "Volume des commandes",
        caption: null,
        element_sha256: CHART_HASH,
        anchor: "lead:0001",
        proposable: false,
        refusal_reason: "note_chart_without_evidence",
      },
      {
        kind: "figure",
        key: "figure-c2",
        title: "Topologie C2",
        caption: null,
        element_sha256: OTHER_HASH,
        anchor: "section:2:paragraph:0007",
        proposable: true,
        refusal_reason: null,
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

function installSelectorFetch(
  handler: (request: {
    url: string;
    init: RequestInit | undefined;
  }) => Response,
) {
  return installFetch((request) => {
    if (
      request.url === `/api/investigations/${INVESTIGATION_ID}/article-anchors`
    ) {
      return jsonResponse(makeCatalog());
    }
    return handler(request);
  });
}

function excerptBody({
  offset,
  length,
  totalSize,
  view,
  excerptSha = EXCERPT_HASH,
  text,
  hex,
}: {
  offset: number;
  length: number;
  totalSize: number;
  view: "text" | "hex";
  excerptSha?: string;
  text?: string;
  hex?: string;
}) {
  return {
    view,
    offset,
    length,
    total_size: totalSize,
    blob_sha256: BLOB_HASH,
    excerpt_sha256: excerptSha,
    untrusted_data: true,
    ...(view === "text" ? { text: text ?? "" } : { hex: hex ?? "" }),
  };
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

  it("grise un élément M23 non proposable avec la raison serveur traduite", async () => {
    installAnchors();
    renderSelection();

    const elementSelect = await screen.findByLabelText("Élément à référencer");
    const refused = within(elementSelect).getByRole("option", {
      name: /Volume des commandes.*non proposable : graphique sans preuve/,
    });
    expect(refused).toBeDisabled();
    expect(
      screen.getByText(/Un élément M23 de cette ancre est grisé/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Référencer cet élément" }),
    ).toBeDisabled();
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

  it("ajoute une plage choisie au clavier avec les métadonnées renvoyées par le serveur", async () => {
    const user = userEvent.setup();
    const serverHash = "9".repeat(64);
    let textRangeRequests = 0;
    const recorder = installSelectorFetch((request) => {
      const url = new URL(request.url, "http://autowork.test");
      const offset = Number(url.searchParams.get("offset"));
      const length = Number(url.searchParams.get("length"));
      const view = url.searchParams.get("view");
      if (view === "hex") {
        return jsonResponse(
          excerptBody({
            offset,
            length,
            totalSize: 3,
            view: "hex",
            hex: "00000000  61",
          }),
        );
      }
      textRangeRequests += 1;
      return jsonResponse(
        excerptBody({
          offset,
          length,
          totalSize: 3,
          view: "text",
          excerptSha: textRangeRequests === 1 ? "8".repeat(64) : serverHash,
          text: "abc",
        }),
      );
    });
    const { onChange } = renderSelection();

    await user.selectOptions(
      await screen.findByLabelText("Observation source"),
      OBSERVATION_ID,
    );
    const excerptElement = await screen.findByRole("textbox", {
      name: "Extrait canonique en texte",
    });
    expect(excerptElement).toBeInstanceOf(HTMLTextAreaElement);
    if (!(excerptElement instanceof HTMLTextAreaElement)) {
      throw new Error("the canonical excerpt should be a textarea");
    }
    const excerpt = excerptElement;
    await user.click(excerpt);
    await user.keyboard(
      "{Home}{Shift>}{ArrowRight}{ArrowRight}{ArrowRight}{/Shift}",
    );
    expect([excerpt.selectionStart, excerpt.selectionEnd]).toEqual([0, 3]);
    expect(
      await screen.findByText("3 octets à partir de 0x0000."),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Offset (octets)")).toBeNull();
    await user.click(
      screen.getByRole("button", { name: "Vérifier et référencer la plage" }),
    );

    await vi.waitFor(() => {
      expect(onChange).toHaveBeenLastCalledWith([
        {
          block_type: "canonical_code",
          observation_id: OBSERVATION_ID,
          blob_sha256: BLOB_HASH,
          offset: 0,
          length: 3,
          excerpt_sha256: serverHash,
          language: "python",
        },
      ]);
    });
    expect(textRangeRequests).toBe(2);
    expect(recorder.calls.map((request) => request.url)).toContain(
      `/api/investigations/${INVESTIGATION_ID}/observations/${OBSERVATION_ID}/excerpt?offset=0&length=3&view=text`,
    );
  });

  it("passe automatiquement en hex quand le serveur refuse la vue texte", async () => {
    const user = userEvent.setup();
    const requests: string[] = [];
    installSelectorFetch((request) => {
      requests.push(request.url);
      const url = new URL(request.url, "http://autowork.test");
      const offset = Number(url.searchParams.get("offset"));
      const length = Number(url.searchParams.get("length"));
      if (url.searchParams.get("view") === "text") {
        return errorResponse(422, "excerpt_not_safe_text");
      }
      return jsonResponse(
        excerptBody({
          offset,
          length,
          totalSize: 2,
          view: "hex",
          hex: "00000000  ff 00",
        }),
      );
    });
    renderSelection();

    await user.selectOptions(
      await screen.findByLabelText("Observation source"),
      OBSERVATION_ID,
    );
    const hexExcerpt = await screen.findByRole("textbox", {
      name: "Extrait canonique en hexadécimal",
    });
    expect(hexExcerpt).toHaveValue("00000000  ff 00");
    expect(screen.getByText(/La vue texte a été refusée/)).toHaveTextContent(
      "excerpt_not_safe_text",
    );
    await user.click(hexExcerpt);
    await user.keyboard("{Control>}a{/Control}");
    expect(
      screen.getByText("2 octets à partir de 0x0000."),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Texte" }));
    expect(
      await screen.findByRole("textbox", {
        name: "Extrait canonique en hexadécimal",
      }),
    ).toBeInTheDocument();
    expect(requests).toContain(
      `/api/investigations/${INVESTIGATION_ID}/observations/${OBSERVATION_ID}/excerpt?offset=0&length=2&view=text`,
    );
  });

  it("pagine des fenêtres de 4096 octets au plus", async () => {
    const user = userEvent.setup();
    const requests: string[] = [];
    installSelectorFetch((request) => {
      requests.push(request.url);
      const url = new URL(request.url, "http://autowork.test");
      const offset = Number(url.searchParams.get("offset"));
      const length = Number(url.searchParams.get("length"));
      const view = url.searchParams.get("view");
      if (view === "hex") {
        return jsonResponse(
          excerptBody({
            offset,
            length,
            totalSize: 5000,
            view: "hex",
            hex: "00000000  61",
          }),
        );
      }
      return jsonResponse(
        excerptBody({
          offset,
          length,
          totalSize: 5000,
          view: "text",
          text: "a".repeat(length),
        }),
      );
    });
    renderSelection();

    await user.selectOptions(
      await screen.findByLabelText("Observation source"),
      OBSERVATION_ID,
    );
    expect(
      await screen.findByText(/Fenêtre de 4096 octets/),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Fenêtre suivante" }));
    expect(
      await screen.findByText(/Fenêtre de 904 octets/),
    ).toBeInTheDocument();
    expect(requests).toContain(
      `/api/investigations/${INVESTIGATION_ID}/observations/${OBSERVATION_ID}/excerpt?offset=4096&length=904&view=text`,
    );
    expect(
      requests
        .map((url) =>
          Number(
            new URL(url, "http://autowork.test").searchParams.get("length"),
          ),
        )
        .every((length) => length <= 4096),
    ).toBe(true);
    await user.click(
      screen.getByRole("button", { name: "Fenêtre précédente" }),
    );
    expect(
      await screen.findByText(/Fenêtre de 4096 octets/),
    ).toBeInTheDocument();
  });

  it("affiche le refus note_code_ref_stale renvoyé pendant la relecture exacte", async () => {
    const user = userEvent.setup();
    installSelectorFetch((request) => {
      const url = new URL(request.url, "http://autowork.test");
      const offset = Number(url.searchParams.get("offset"));
      const length = Number(url.searchParams.get("length"));
      if (url.searchParams.get("view") === "hex") {
        return jsonResponse(
          excerptBody({
            offset,
            length,
            totalSize: 3,
            view: "hex",
            hex: "00000000  61",
          }),
        );
      }
      if (length === 3) {
        return jsonResponse(
          excerptBody({
            offset,
            length,
            totalSize: 3,
            view: "text",
            text: "abc",
          }),
        );
      }
      return errorResponse(409, "note_code_ref_stale");
    });
    renderSelection();

    await user.selectOptions(
      await screen.findByLabelText("Observation source"),
      OBSERVATION_ID,
    );
    const excerpt = await screen.findByRole("textbox", {
      name: "Extrait canonique en texte",
    });
    await user.click(excerpt);
    await user.keyboard("{Home}{Shift>}{ArrowRight}{/Shift}");
    await user.click(
      screen.getByRole("button", { name: "Vérifier et référencer la plage" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "note_code_ref_stale (HTTP 409)",
    );
  });

  it("affiche littéralement le texte CTI non fiable sans l’interpréter en HTML", async () => {
    const user = userEvent.setup();
    const untrusted = '<img onerror="alert(1)">';
    installSelectorFetch((request) => {
      const url = new URL(request.url, "http://autowork.test");
      const offset = Number(url.searchParams.get("offset"));
      const length = Number(url.searchParams.get("length"));
      const view = url.searchParams.get("view");
      return jsonResponse(
        excerptBody({
          offset,
          length,
          totalSize: untrusted.length,
          view: view === "hex" ? "hex" : "text",
          text: untrusted,
          hex: "00000000  3c 69 6d 67",
        }),
      );
    });
    renderSelection();

    await user.selectOptions(
      await screen.findByLabelText("Observation source"),
      OBSERVATION_ID,
    );
    const excerpt = await screen.findByRole("textbox", {
      name: "Extrait canonique en texte",
    });
    expect(excerpt).toHaveValue(untrusted);
    expect(screen.queryByRole("img")).toBeNull();
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
          object_id: IOC_OBJECT_ID,
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

  it("rend les cross-références, le code, le tableau IOC et la figure avec provenance", async () => {
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
    const table = screen.getByRole("table", { name: "Tableau IOC (1)" });
    expect(table.closest("ul")).toBeNull();
    expect(screen.getByText(IOC_OBJECT_ID)).not.toBeVisible();
    const tableDetails = table
      .closest(".note-content-blocks__resolved-item")
      ?.querySelector("details");
    expect(tableDetails).not.toBeNull();
    await userEvent.setup().click(
      within(tableDetails as HTMLElement).getByText("Provenance technique", {
        selector: "summary",
      }),
    );
    expect(
      within(tableDetails as HTMLElement).getByText(IOC_OBJECT_ID),
    ).toBeVisible();
    expect(
      within(tableDetails as HTMLElement).getByText(OBSERVATION_ID),
    ).toBeVisible();
    expect(
      screen.getByText(/Séquence des échanges observée/),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Base de publication épinglée · version 3"),
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
