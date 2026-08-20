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
});
