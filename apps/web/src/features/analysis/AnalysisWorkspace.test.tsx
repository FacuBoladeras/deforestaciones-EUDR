import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AnalysisWorkspace } from "./AnalysisWorkspace";
import type { PolygonFeature } from "../../domain/geojson";

const drawnGeometry: PolygonFeature = {
  type: "Feature",
  properties: {},
  geometry: {
    type: "Polygon",
    coordinates: [
      [
        [-60.3, -32.1],
        [-60.2, -32.1],
        [-60.2, -32.0],
        [-60.3, -32.1],
      ],
    ],
  },
};

vi.mock("../../components/GeometryMap", () => ({
  GeometryMap: ({ onGeometryChange }: { onGeometryChange: (value: PolygonFeature) => void }) => (
    <button type="button" onClick={() => onGeometryChange(drawnGeometry)}>
      Dibujar fixture
    </button>
  ),
}));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  window.history.replaceState(null, "", "/");
});

function renderWorkspace() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AnalysisWorkspace />
    </QueryClientProvider>,
  );
}

describe("AnalysisWorkspace", () => {
  it("retoma desde la URL el seguimiento de un análisis en ejecución", async () => {
    window.history.replaceState(
      null,
      "",
      "/?analysis=bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    );
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          analysis_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
          status: "running",
          stage: "full_pipeline",
          created_at: "2026-08-17T00:00:00Z",
          started_at: "2026-08-17T00:00:01Z",
          updated_at: "2026-08-17T00:00:01Z",
          completed_at: null,
          attempt: 1,
          safe_error_code: null,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    renderWorkspace();

    expect(screen.getByText("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")).toBeInTheDocument();
    expect(await screen.findByText("Análisis en curso")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/analyses/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
      undefined,
    );
  });

  it("valida el dibujo antes de habilitar la creación del análisis", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          geometry_type: "Polygon",
          source_crs: "EPSG:4326",
          valid: true,
          repair_required: false,
          area_ha_estimate: 25.4,
          warnings: [],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    renderWorkspace();
    expect(screen.getByRole("button", { name: "Crear análisis" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Dibujar fixture" }));
    await user.type(screen.getByLabelText("Identificador del establecimiento"), "campo-1");
    await user.click(screen.getByRole("button", { name: "Validar geometría" }));

    expect(await screen.findByText("25,40 ha")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Crear análisis" })).toBeEnabled();
  });

  it("carga GeoJSON y muestra el estado operativo retornado por la API", async () => {
    const user = userEvent.setup();
    const responses = [
      {
        geometry_type: "Polygon",
        source_crs: "EPSG:4326",
        valid: true,
        repair_required: false,
        area_ha_estimate: 25.4,
        warnings: [],
      },
      {
        analysis_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        status: "queued",
        status_url: "/api/v1/analyses/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        created_at: "2026-08-17T00:00:00Z",
      },
      {
        analysis_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        status: "queued",
        stage: "queued",
        created_at: "2026-08-17T00:00:00Z",
        started_at: null,
        updated_at: "2026-08-17T00:00:00Z",
        completed_at: null,
        attempt: 0,
        safe_error_code: null,
      },
    ];
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => {
      const payload = responses.shift();
      return new Response(JSON.stringify(payload), {
        status: payload && "status_url" in payload ? 202 : 200,
        headers: { "Content-Type": "application/json" },
      });
    });

    renderWorkspace();
    const file = new File([JSON.stringify(drawnGeometry)], "campo.geojson", {
      type: "application/geo+json",
    });
    fireEvent.change(screen.getByLabelText("Cargar archivo GeoJSON"), {
      target: { files: [file] },
    });
    await user.type(screen.getByLabelText("Identificador del establecimiento"), "campo-1");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Validar geometría" })).toBeEnabled(),
    );
    await user.click(screen.getByRole("button", { name: "Validar geometría" }));
    await user.click(await screen.findByRole("button", { name: "Crear análisis" }));

    await waitFor(() => expect(screen.getByText("En cola")).toBeInTheDocument());
    expect(screen.getByText("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Progreso estimado del análisis" })).toHaveAttribute(
      "aria-valuenow",
      "6",
    );
    expect(screen.getByText(/El 100 % sólo se confirma/)).toBeInTheDocument();
  });

  it("ofrece el informe PDF cuando el worker terminó de publicarlo", async () => {
    window.history.replaceState(
      null,
      "",
      "/?analysis=aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    );
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      const payload = url.endsWith("/report")
        ? { analysis: { status: "review_required" }, headline_metrics: {} }
        : url.includes("/events") || url.includes("/assets")
          ? { items: [], page: 1, page_size: 100, total: 0, next_page: null }
          : {
              analysis_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
              status: "completed",
              stage: "completed",
              created_at: "2026-08-17T00:00:00Z",
              started_at: "2026-08-17T00:00:01Z",
              updated_at: "2026-08-17T00:01:00Z",
              completed_at: "2026-08-17T00:01:00Z",
              attempt: 1,
              safe_error_code: null,
            };
      return new Response(JSON.stringify(payload), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });

    renderWorkspace();

    const link = await screen.findByRole("link", { name: "Descargar informe PDF" });
    expect(link).toHaveAttribute(
      "href",
      "/api/v1/analyses/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/report.pdf",
    );
  });
});
