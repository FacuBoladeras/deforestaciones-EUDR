import type { PolygonFeature } from "../domain/geojson";

export interface GeometryValidation {
  geometry_type: "Polygon" | "MultiPolygon";
  source_crs: "EPSG:4326";
  valid: true;
  repair_required: boolean;
  area_ha_estimate: number;
  warnings: string[];
}

export interface AnalysisCreated {
  analysis_id: string;
  status: string;
  status_url: string;
  created_at: string;
}

export interface AnalysisStatus {
  analysis_id: string;
  status:
    | "queued"
    | "validating"
    | "running"
    | "completed"
    | "partial"
    | "failed"
    | "cancelling"
    | "cancelled";
  stage: string;
  created_at: string;
  started_at: string | null;
  updated_at: string;
  completed_at: string | null;
  attempt: number;
  safe_error_code: string | null;
}

export interface Page<T> {
  items: T[];
  page: number;
  page_size: number;
  total: number;
  next_page: number | null;
}

export interface AnalysisAsset {
  asset_id: string;
  category: string;
  component: string | null;
  event_id: string | null;
  media_type: string;
  size_bytes: number;
  sha256: string;
  download_url: string;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details: string[] = [],
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export class ApiClient {
  constructor(private readonly baseUrl = "") {}

  validateGeometry(geometry: PolygonFeature): Promise<GeometryValidation> {
    return this.request("/api/v1/geometries/validate", {
      method: "POST",
      headers: { "Content-Type": "application/geo+json" },
      body: JSON.stringify(geometry),
    });
  }

  createAnalysis(input: {
    establishmentId: string;
    geometry: PolygonFeature;
  }): Promise<AnalysisCreated> {
    return this.request("/api/v1/analyses", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": crypto.randomUUID(),
      },
      body: JSON.stringify({
        establishment_id: input.establishmentId,
        geometry: input.geometry,
        declared_land_use: "unknown",
        declared_context_source: "user_declared",
      }),
    });
  }

  getAnalysis(analysisId: string): Promise<AnalysisStatus> {
    return this.request(`/api/v1/analyses/${analysisId}`);
  }

  cancelAnalysis(analysisId: string): Promise<AnalysisStatus> {
    return this.request(`/api/v1/analyses/${analysisId}/cancel`, { method: "POST" });
  }

  getReport(analysisId: string): Promise<Record<string, unknown>> {
    return this.request(`/api/v1/analyses/${analysisId}/report`);
  }

  getEvents(analysisId: string): Promise<Page<Record<string, unknown>>> {
    return this.requestAllPages(`/api/v1/analyses/${analysisId}/events`);
  }

  getCandidates(analysisId: string): Promise<Page<Record<string, unknown>>> {
    return this.requestAllPages(`/api/v1/analyses/${analysisId}/candidates`);
  }

  getAssets(analysisId: string): Promise<Page<AnalysisAsset>> {
    return this.requestAllPages(`/api/v1/analyses/${analysisId}/assets`);
  }

  packageUrl(analysisId: string): string {
    return this.url(`/api/v1/analyses/${analysisId}/download`);
  }

  reportPdfUrl(analysisId: string): string {
    return this.url(`/api/v1/analyses/${analysisId}/report.pdf`);
  }

  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await fetch(this.url(path), init);
    const contentType = response.headers.get("Content-Type") ?? "";
    const payload: unknown = contentType.includes("json") ? await response.json() : null;
    if (!response.ok) {
      const error = extractError(payload);
      throw new ApiError(response.status, error.code, error.message, error.details);
    }
    return payload as T;
  }

  private async requestAllPages<T>(path: string): Promise<Page<T>> {
    const pageSize = 100;
    const items: T[] = [];
    let requestedPage = 1;
    let declaredTotal: number | null = null;

    while (true) {
      const page = await this.request<Page<T>>(
        `${path}?page=${requestedPage}&page_size=${pageSize}`,
      );
      if (
        page.page !== requestedPage ||
        !Number.isInteger(page.total) ||
        page.total < 0 ||
        (declaredTotal !== null && page.total !== declaredTotal)
      ) {
        throw new Error("pagination_inconsistent");
      }
      declaredTotal ??= page.total;
      items.push(...page.items);
      if (page.next_page == null) break;
      if (!Number.isInteger(page.next_page) || page.next_page <= requestedPage) {
        throw new Error("pagination_inconsistent");
      }
      requestedPage = page.next_page;
    }

    if (items.length !== declaredTotal) throw new Error("pagination_incomplete");
    return {
      items,
      page: 1,
      page_size: pageSize,
      total: declaredTotal,
      next_page: null,
    };
  }

  private url(path: string): string {
    return `${this.baseUrl.replace(/\/$/, "")}${path}`;
  }
}

function extractError(payload: unknown): {
  code: string;
  message: string;
  details: string[];
} {
  if (isRecord(payload) && isRecord(payload.error)) {
    return {
      code: typeof payload.error.code === "string" ? payload.error.code : "request_failed",
      message:
        typeof payload.error.message === "string"
          ? payload.error.message
          : "La solicitud no pudo completarse",
      details: Array.isArray(payload.error.details)
        ? payload.error.details.filter((item): item is string => typeof item === "string")
        : [],
    };
  }
  return {
    code: "request_failed",
    message: "La solicitud no pudo completarse",
    details: [],
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
