import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
  GeometryMap: ({
    onEditingStart,
    onEditingCancel,
    onGeometryChange,
    onGeometryClear,
  }: {
    onEditingStart: () => void;
    onEditingCancel: () => void;
    onGeometryChange: (value: PolygonFeature) => void;
    onGeometryClear: () => void;
  }) => (
    <div>
      <button
        type="button"
        onClick={() => {
          onEditingStart();
          onGeometryChange(drawnGeometry);
        }}
      >
        Dibujar fixture
      </button>
      <button type="button" onClick={onEditingStart}>
        Volver a editar fixture
      </button>
      <button type="button" onClick={onEditingCancel}>
        Cancelar edición fixture
      </button>
      <button type="button" onClick={onGeometryClear}>
        Borrar fixture
      </button>
    </div>
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

  it("mantiene el dibujo pendiente hasta que la persona acepta la geometría", async () => {
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
    expect(screen.getByText(/pendiente de aceptación/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Crear análisis" })).toBeDisabled();
    await user.type(screen.getByLabelText("Identificador del establecimiento"), "campo-1");
    await user.click(screen.getByRole("button", { name: "Aceptar geometría" }));

    expect(await screen.findByText("25,40 ha")).toBeInTheDocument();
    expect(screen.getByText(/geometría aceptada y validada/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Crear análisis" })).toBeEnabled();
  });

  it("suspende el envío al editar, restaura al cancelar y permite borrar", async () => {
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
    await user.click(screen.getByRole("button", { name: "Dibujar fixture" }));
    await user.type(screen.getByLabelText("Identificador del establecimiento"), "campo-1");
    await user.click(screen.getByRole("button", { name: "Aceptar geometría" }));
    expect(await screen.findByRole("button", { name: "Crear análisis" })).toBeEnabled();

    await user.click(screen.getByRole("button", { name: "Volver a editar fixture" }));
    expect(screen.getByRole("button", { name: "Crear análisis" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Cancelar edición fixture" }));
    expect(screen.getByRole("button", { name: "Crear análisis" })).toBeEnabled();
    expect(screen.getByText(/geometría aceptada y validada/i)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Volver a editar fixture" }));

    await user.click(screen.getByRole("button", { name: "Borrar fixture" }));
    expect(screen.getByText(/aún no hay geometría/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Aceptar geometría" })).toBeDisabled();
  });

  it("ignora una validación tardía si la geometría se editó mientras esperaba", async () => {
    const user = userEvent.setup();
    let resolveValidation: ((response: Response) => void) | undefined;
    vi.spyOn(globalThis, "fetch").mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          resolveValidation = resolve;
        }),
    );

    renderWorkspace();
    await user.click(screen.getByRole("button", { name: "Dibujar fixture" }));
    await user.type(screen.getByLabelText("Identificador del establecimiento"), "campo-1");
    await user.click(screen.getByRole("button", { name: "Aceptar geometría" }));
    expect(screen.getByText(/validando geometría/i)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Volver a editar fixture" }));
    await act(async () => {
      resolveValidation?.(
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
      await Promise.resolve();
    });

    await waitFor(() => expect(screen.queryByText("25,40 ha")).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Crear análisis" })).toBeDisabled();
    expect(screen.getByText(/pendiente de aceptación/i)).toBeInTheDocument();
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
      expect(screen.getByRole("button", { name: "Aceptar geometría" })).toBeEnabled(),
    );
    expect(screen.getByText(/pendiente de aceptación/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Aceptar geometría" }));
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
        ? { analysis: { overall_status: "review_required" }, headline_metrics: {} }
        : url.includes("/events")
          ? {
              items: [{ candidate_id: "PDE-1", event_id: "EVT-1", area_ha: 1.2 }],
              page: 1,
              page_size: 100,
              total: 1,
              next_page: null,
            }
          : url.includes("/candidates")
            ? {
                items: Array.from({ length: 13 }, (_, index) => ({
                    candidate_id: `PDE-${index + 2}`,
                    event_id: null,
                    record_type: "disturbance_candidate",
                    interpretation_status: "subthreshold_conversion_evidence",
                    candidate_area_ha: 1.2,
                    conjunctive_conversion_evidence_area_ha: 0.4,
                    likely_conversion_area_ha: 0,
                  })),
                page: 1,
                page_size: 100,
                total: 13,
                next_page: null,
              }
            : url.includes("/assets")
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
    expect(await screen.findByText("Eventos probables (1)")).toBeInTheDocument();
    expect(screen.getByText("Perturbaciones candidatas (13)")).toBeInTheDocument();
    expect(screen.getByText("EVT-1")).toBeInTheDocument();
    expect(screen.getByText("PDE-2")).toBeInTheDocument();
    expect(screen.getByText("PDE-14")).toBeInTheDocument();
    expect(screen.getAllByText("Evidencia conjunta bajo umbral; no es evento")).toHaveLength(13);
    expect(screen.getAllByText("1,20 ha")).toHaveLength(13);
    expect(screen.getByText("Estado científico: review_required")).toBeInTheDocument();
  });

  it("no presenta un error de inventario como ausencia de eventos", async () => {
    window.history.replaceState(
      null,
      "",
      "/?analysis=aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    );
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/report")) {
        return new Response(
          JSON.stringify({ error: { code: "result_unavailable", message: "Informe no disponible" } }),
          { status: 503, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url.includes("/events") || url.includes("/candidates") || url.includes("/assets")) {
        return new Response(
          JSON.stringify({ items: [], page: 1, page_size: 100, total: 0, next_page: null }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response(
        JSON.stringify({
          analysis_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
          status: "completed",
          stage: "completed",
          created_at: "2026-08-17T00:00:00Z",
          started_at: "2026-08-17T00:00:01Z",
          updated_at: "2026-08-17T00:01:00Z",
          completed_at: "2026-08-17T00:01:00Z",
          attempt: 1,
          safe_error_code: null,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    });

    renderWorkspace();

    expect(await screen.findByText("Informe no disponible")).toBeInTheDocument();
    expect(screen.queryByText(/No se detectaron eventos/)).not.toBeInTheDocument();
    expect(screen.queryByText(/No hay perturbaciones candidatas/)).not.toBeInTheDocument();
  });
});
