import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient, ApiError } from "./client";
import type { PolygonFeature } from "../domain/geojson";

const geometry: PolygonFeature = {
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

afterEach(() => vi.restoreAllMocks());

describe("ApiClient", () => {
  it("envía GeoJSON a validación y conserva la respuesta tipada", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          geometry_type: "Polygon",
          source_crs: "EPSG:4326",
          valid: true,
          repair_required: false,
          area_ha_estimate: 12.5,
          warnings: [],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    const result = await new ApiClient().validateGeometry(geometry);

    expect(result.area_ha_estimate).toBe(12.5);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/geometries/validate",
      expect.objectContaining({ method: "POST", body: JSON.stringify(geometry) }),
    );
  });

  it("crea análisis con UUID idempotente generado por el cliente", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          analysis_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
          status: "queued",
          status_url: "/api/v1/analyses/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
          created_at: "2026-08-17T00:00:00Z",
        }),
        { status: 202, headers: { "Content-Type": "application/json" } },
      ),
    );

    await new ApiClient().createAnalysis({ establishmentId: "campo-1", geometry });

    const headers = fetchMock.mock.calls[0]?.[1]?.headers as Record<string, string>;
    expect(headers["Idempotency-Key"]).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
  });

  it("convierte errores seguros del backend en ApiError", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          error: {
            code: "invalid_geometry",
            message: "La geometría no es utilizable",
            details: ["fuera de la jurisdicción"],
          },
        }),
        { status: 422, headers: { "Content-Type": "application/json" } },
      ),
    );

    await expect(new ApiClient().validateGeometry(geometry)).rejects.toEqual(
      expect.objectContaining({
        name: "ApiError",
        code: "invalid_geometry",
        status: 422,
      } satisfies Partial<ApiError>),
    );
  });

  it("construye la descarga directa del informe PDF", () => {
    const api = new ApiClient("http://127.0.0.1:8000/");

    expect(api.reportPdfUrl("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")).toBe(
      "http://127.0.0.1:8000/api/v1/analyses/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/report.pdf",
    );
  });

  it("consulta perturbaciones candidatas en un recurso separado de eventos", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ items: [], page: 1, page_size: 100, total: 0 }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    await new ApiClient().getCandidates("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/analyses/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/candidates?page=1&page_size=100",
      undefined,
    );
  });

  it("recorre todas las páginas de eventos y conserva el total declarado", async () => {
    const firstItems = Array.from({ length: 100 }, (_, index) => ({ event_id: `event-${index}` }));
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      const payload = url.includes("page=2")
        ? {
            items: [{ event_id: "event-100" }],
            page: 2,
            page_size: 100,
            total: 101,
            next_page: null,
          }
        : { items: firstItems, page: 1, page_size: 100, total: 101, next_page: 2 };
      return new Response(JSON.stringify(payload), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });

    const page = await new ApiClient().getEvents(
      "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    );

    expect(page.items).toHaveLength(101);
    expect(page.items.at(-1)).toEqual({ event_id: "event-100" });
    expect(page.total).toBe(101);
    expect(page.next_page).toBeNull();
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/v1/analyses/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/events?page=2&page_size=100",
      undefined,
    );
  });

  it("rechaza paginación inconsistente en lugar de mostrar un inventario incompleto", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ items: [], page: 1, page_size: 100, total: 1, next_page: null }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    await expect(
      new ApiClient().getCandidates("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
    ).rejects.toThrow("pagination_incomplete");
  });

  it("recorre todas las páginas de assets", async () => {
    const firstItems = Array.from({ length: 100 }, (_, index) => ({
      asset_id: `asset-${index}`,
      category: "figure",
      component: null,
      event_id: null,
      media_type: "image/png",
      size_bytes: 1,
      sha256: "a".repeat(64),
      download_url: `/asset-${index}`,
    }));
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const second = String(input).includes("page=2");
      return new Response(
        JSON.stringify(
          second
            ? {
                items: [
                  {
                    asset_id: "asset-100",
                    category: "figure",
                    component: null,
                    event_id: null,
                    media_type: "image/png",
                    size_bytes: 1,
                    sha256: "b".repeat(64),
                    download_url: "/asset-100",
                  },
                ],
                page: 2,
                page_size: 100,
                total: 101,
                next_page: null,
              }
            : { items: firstItems, page: 1, page_size: 100, total: 101, next_page: 2 },
        ),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    });

    const page = await new ApiClient().getAssets("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa");

    expect(page.items).toHaveLength(101);
    expect(page.items.at(-1)?.asset_id).toBe("asset-100");
  });
});
