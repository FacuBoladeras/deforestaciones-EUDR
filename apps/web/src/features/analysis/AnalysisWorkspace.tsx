import { useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  ApiClient,
  ApiError,
  type AnalysisStatus,
  type GeometryValidation,
} from "../../api/client";
import { GeometryMap } from "../../components/GeometryMap";
import { parsePolygonDocument, type PolygonFeature } from "../../domain/geojson";
import { useEstimatedProgress } from "./useEstimatedProgress";

const ACTIVE_STATUSES = new Set(["queued", "validating", "running", "cancelling"]);
const RESULT_STATUSES = new Set(["completed", "partial"]);
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function AnalysisWorkspace() {
  const api = useMemo(() => new ApiClient(), []);
  const queryClient = useQueryClient();
  const [draftGeometry, setDraftGeometry] = useState<PolygonFeature | null>(null);
  const draftGeometryRef = useRef<PolygonFeature | null>(null);
  const geometryRevisionRef = useRef(0);
  const [acceptedGeometry, setAcceptedGeometry] = useState<PolygonFeature | null>(null);
  const [geometryEditing, setGeometryEditing] = useState(false);
  const [validation, setValidation] = useState<GeometryValidation | null>(null);
  const [establishmentId, setEstablishmentId] = useState("");
  const [analysisId, setAnalysisId] = useState<string | null>(analysisIdFromLocation);
  const [localError, setLocalError] = useState<string | null>(null);

  const validationMutation = useMutation({
    mutationFn: ({ candidate }: { candidate: PolygonFeature; revision: number }) =>
      api.validateGeometry(candidate),
    onSuccess: (result, { candidate, revision }) => {
      if (draftGeometryRef.current !== candidate || geometryRevisionRef.current !== revision) return;
      setValidation(result);
      setAcceptedGeometry(candidate);
    },
  });
  const createMutation = useMutation({
    mutationFn: () => {
      if (!acceptedGeometry) throw new Error("geometry_missing");
      return api.createAnalysis({ establishmentId, geometry: acceptedGeometry });
    },
    onSuccess: (created) => trackAnalysis(created.analysis_id),
  });
  const statusQuery = useQuery({
    queryKey: ["analysis-status", analysisId],
    queryFn: () => api.getAnalysis(analysisId ?? ""),
    enabled: Boolean(analysisId),
    refetchInterval: (query) =>
      ACTIVE_STATUSES.has(query.state.data?.status ?? "") ? 2_000 : false,
  });
  const canReadResults = RESULT_STATUSES.has(statusQuery.data?.status ?? "");
  const reportQuery = useQuery({
    queryKey: ["analysis-report", analysisId],
    queryFn: () => api.getReport(analysisId ?? ""),
    enabled: Boolean(analysisId && canReadResults),
  });
  const eventsQuery = useQuery({
    queryKey: ["analysis-events", analysisId],
    queryFn: () => api.getEvents(analysisId ?? ""),
    enabled: Boolean(analysisId && canReadResults),
  });
  const candidatesQuery = useQuery({
    queryKey: ["analysis-candidates", analysisId],
    queryFn: () => api.getCandidates(analysisId ?? ""),
    enabled: Boolean(analysisId && canReadResults),
  });
  const assetsQuery = useQuery({
    queryKey: ["analysis-assets", analysisId],
    queryFn: () => api.getAssets(analysisId ?? ""),
    enabled: Boolean(analysisId && canReadResults),
  });
  const cancelMutation = useMutation({
    mutationFn: () => api.cancelAnalysis(analysisId ?? ""),
    onSuccess: (status) => queryClient.setQueryData(["analysis-status", analysisId], status),
  });

  const invalidateAcceptance = () => {
    geometryRevisionRef.current += 1;
    validationMutation.reset();
    setAcceptedGeometry(null);
    setValidation(null);
    trackAnalysis(null);
    setLocalError(null);
  };

  const updateGeometry = (candidate: PolygonFeature) => {
    draftGeometryRef.current = candidate;
    setDraftGeometry(candidate);
    setGeometryEditing(false);
    invalidateAcceptance();
  };

  const startEditingGeometry = () => {
    geometryRevisionRef.current += 1;
    validationMutation.reset();
    setGeometryEditing(true);
    setLocalError(null);
    if (!acceptedGeometry) setValidation(null);
  };

  const cancelEditingGeometry = () => {
    setGeometryEditing(false);
  };

  const clearGeometry = () => {
    draftGeometryRef.current = null;
    setDraftGeometry(null);
    setGeometryEditing(false);
    invalidateAcceptance();
  };

  const trackAnalysis = (identifier: string | null) => {
    setAnalysisId(identifier);
    const url = new URL(window.location.href);
    if (identifier) url.searchParams.set("analysis", identifier);
    else url.searchParams.delete("analysis");
    window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
  };

  const loadFile = async (file: File | undefined) => {
    if (!file) return;
    try {
      updateGeometry(parsePolygonDocument(await file.text()));
    } catch (error) {
      setLocalError(messageFrom(error));
    }
  };

  const acceptGeometry = () => {
    if (draftGeometry) {
      validationMutation.mutate({ candidate: draftGeometry, revision: geometryRevisionRef.current });
    }
  };

  const geometryFeedback = geometryFeedbackFor({
    accepted: Boolean(acceptedGeometry),
    hasDraft: Boolean(draftGeometry),
    validating: validationMutation.isPending,
  });

  const resultQueries = [reportQuery, eventsQuery, candidatesQuery, assetsQuery] as const;
  const resultsLoading = canReadResults && resultQueries.some((query) => query.isPending);
  const resultsReady = canReadResults && resultQueries.every((query) => query.isSuccess);
  const resultQueryError = resultQueries
    .map((query) => mutationError(query.error))
    .find((message): message is string => message !== null);

  const visibleError =
    localError ??
    mutationError(validationMutation.error) ??
    mutationError(createMutation.error) ??
    mutationError(statusQuery.error) ??
    mutationError(cancelMutation.error) ??
    resultQueryError ??
    null;

  return (
    <main className="shell">
      <header className="hero">
        <div>
          <p className="eyebrow">Herramienta interna</p>
          <h1>Evidencia territorial EUDR</h1>
          <p className="hero-copy">
            Cargá o dibujá una geometría, aceptala y seguí el análisis sin ejecutar comandos.
          </p>
        </div>
        <div className="technical-badge">Resultado técnico · No certifica cumplimiento legal</div>
      </header>

      <div className="workspace-grid">
        <GeometryMap
          geometry={draftGeometry}
          status={acceptedGeometry ? "accepted" : draftGeometry ? "draft" : "empty"}
          onEditingStart={startEditingGeometry}
          onEditingCancel={cancelEditingGeometry}
          onGeometryChange={updateGeometry}
          onGeometryClear={clearGeometry}
        />

        <section className="control-card" aria-labelledby="request-title">
          <p className="eyebrow">Solicitud</p>
          <h2 id="request-title">Nuevo análisis</h2>
          <label className="field">
            <span>Identificador del establecimiento</span>
            <input
              value={establishmentId}
              maxLength={200}
              onChange={(event) => setEstablishmentId(event.target.value)}
              placeholder="Ej. establecimiento-2"
            />
          </label>
          <label className="file-field">
            <span>Cargar archivo GeoJSON</span>
            <input
              type="file"
              accept=".geojson,.json,application/geo+json,application/json"
              onChange={(event) => void loadFile(event.target.files?.[0])}
            />
          </label>
          <p className="field-help">Se admite un único Polygon o MultiPolygon en EPSG:4326.</p>

          <div className="button-row">
            <button
              type="button"
              className="button button-secondary"
              disabled={!draftGeometry || Boolean(acceptedGeometry) || validationMutation.isPending}
              onClick={acceptGeometry}
            >
              {validationMutation.isPending ? "Validando…" : "Aceptar geometría"}
            </button>
            <button
              type="button"
              className="button button-primary"
              disabled={
                !acceptedGeometry ||
                geometryEditing ||
                !establishmentId.trim() ||
                createMutation.isPending
              }
              onClick={() => createMutation.mutate()}
            >
              {createMutation.isPending ? "Creando…" : "Crear análisis"}
            </button>
          </div>

          <p
            className={`geometry-feedback geometry-feedback-${geometryFeedback.kind}`}
            role="status"
          >
            <strong>{geometryFeedback.title}</strong>
            <span>{geometryFeedback.detail}</span>
          </p>
          {validation && (
            <div className="validation-card">
              <strong>{formatArea(validation.area_ha_estimate)}</strong>
              <span>{validation.repair_required ? "Requiere reparación controlada" : "Geometría válida"}</span>
              {validation.warnings.map((warning) => (
                <small key={warning}>{warning}</small>
              ))}
            </div>
          )}
          {visibleError && <p className="error-message">{visibleError}</p>}
        </section>
      </div>

      {analysisId && (
        <StatusPanel
          key={analysisId}
          analysisId={analysisId}
          status={statusQuery.data?.status ?? "queued"}
          stage={statusQuery.data?.stage ?? "queued"}
          errorCode={statusQuery.data?.safe_error_code ?? null}
          canCancel={ACTIVE_STATUSES.has(statusQuery.data?.status ?? "queued")}
          onCancel={() => cancelMutation.mutate()}
        />
      )}

      {analysisId && resultsLoading && (
        <p className="results-loading" role="status">
          Cargando resultados…
        </p>
      )}

      {analysisId && resultsReady && (
        <ResultsPanel
          analysisId={analysisId}
          api={api}
          report={reportQuery.data}
          events={eventsQuery.data?.items ?? []}
          candidates={candidatesQuery.data?.items ?? []}
          assets={assetsQuery.data?.items ?? []}
        />
      )}
    </main>
  );
}

function StatusPanel({
  analysisId,
  status,
  stage,
  errorCode,
  canCancel,
  onCancel,
}: {
  analysisId: string;
  status: AnalysisStatus["status"];
  stage: string;
  errorCode: string | null;
  canCancel: boolean;
  onCancel: () => void;
}) {
  const progress = useEstimatedProgress(status);
  const confirmedComplete = RESULT_STATUSES.has(status);
  const stopped = status === "failed" || status === "cancelled";
  return (
    <section className="status-card" aria-live="polite">
      <div>
        <p className="eyebrow">Análisis</p>
        <h2>{statusLabel(status)}</h2>
        <p className="analysis-id">{analysisId}</p>
        <div className="analysis-progress">
          <div className="progress-caption">
            <span>{confirmedComplete ? "Finalización confirmada" : "Progreso estimado"}</span>
            <strong>{progress} %</strong>
          </div>
          <div
            className="progress-track"
            role="progressbar"
            aria-label="Progreso estimado del análisis"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={progress}
            aria-valuetext={confirmedComplete ? "Finalización confirmada" : `${progress} % estimado`}
          >
            <span className="progress-fill" style={{ width: `${progress}%` }} />
          </div>
          <small className="progress-note">
            {confirmedComplete
              ? "La API confirmó que el pipeline terminó y publicó resultados."
              : stopped
                ? "La API informó que el proceso se detuvo antes de finalizar."
                : "El 100 % sólo se confirma cuando la API informa que el pipeline terminó."}
          </small>
        </div>
      </div>
      <div className="status-meta">
        <span>Etapa: {stageLabel(stage)}</span>
        {errorCode && <span>Error seguro: {errorCode}</span>}
        {canCancel && (
          <button type="button" className="button button-ghost" onClick={onCancel}>
            Cancelar
          </button>
        )}
      </div>
    </section>
  );
}

function ResultsPanel({
  analysisId,
  api,
  report,
  events,
  candidates,
  assets,
}: {
  analysisId: string;
  api: ApiClient;
  report: Record<string, unknown> | undefined;
  events: Record<string, unknown>[];
  candidates: Record<string, unknown>[];
  assets: Awaited<ReturnType<ApiClient["getAssets"]>>["items"];
}) {
  const metrics = recordValue(report?.headline_metrics);
  const scientificStatus =
    stringValue(recordValue(report?.analysis)?.overall_status) ?? "No informado";
  const images = assets.filter((asset) => asset.media_type.startsWith("image/"));
  return (
    <section className="results-section">
      <div className="results-heading">
        <div>
          <p className="eyebrow">Resultados</p>
          <h2>Resumen técnico</h2>
          <p>Estado científico: {scientificStatus}</p>
        </div>
        <div className="download-actions">
          <a className="button button-primary" href={api.reportPdfUrl(analysisId)}>
            Descargar informe PDF
          </a>
          <a className="button button-secondary" href={api.packageUrl(analysisId)}>
            Descargar evidencia
          </a>
        </div>
      </div>

      {metrics && (
        <div className="metric-grid">
          {Object.entries(metrics).map(([key, value]) => (
            <article className="metric-card" key={key}>
              <span>{humanize(key)}</span>
              <strong>{formatValue(value)}</strong>
            </article>
          ))}
        </div>
      )}

      <div className="result-grid">
        <div>
          <h3>Eventos probables ({events.length})</h3>
          <div className="event-list">
            {events.map((event, index) => (
              <article className="event-card" key={stringValue(event.event_id) ?? index}>
                <strong>{stringValue(event.event_id) ?? `Evento ${index + 1}`}</strong>
                <span>
                  {interpretationLabel(
                    stringValue(event.interpretation_status) ??
                      stringValue(event.status) ??
                      "conversion_likely",
                  )}
                </span>
                {resultArea(event, "event") !== null && (
                  <small>{formatArea(resultArea(event, "event") ?? 0)}</small>
                )}
              </article>
            ))}
            {events.length === 0 && (
              <p className="empty-state">No se detectaron eventos de conversión probable.</p>
            )}
          </div>
          <h3>Perturbaciones candidatas ({candidates.length})</h3>
          <div className="event-list">
            {candidates.map((candidate, index) => (
              <article
                className="event-card"
                key={stringValue(candidate.candidate_id) ?? index}
              >
                <strong>{stringValue(candidate.candidate_id) ?? `Candidato ${index + 1}`}</strong>
                <span>
                  {interpretationLabel(
                    stringValue(candidate.interpretation_status) ?? "candidate_only",
                  )}
                </span>
                {resultArea(candidate, "candidate") !== null && (
                  <small>{formatArea(resultArea(candidate, "candidate") ?? 0)}</small>
                )}
              </article>
            ))}
            {candidates.length === 0 && (
              <p className="empty-state">No hay perturbaciones candidatas para mostrar.</p>
            )}
          </div>
        </div>
        <div>
          <h3>Figuras ({images.length})</h3>
          <div className="gallery">
            {images.slice(0, 8).map((asset) => (
              <a href={asset.download_url} key={asset.asset_id} className="gallery-item">
                <img src={asset.download_url} alt={asset.category} loading="lazy" />
                <span>{humanize(asset.category)}</span>
              </a>
            ))}
            {images.length === 0 && <p className="empty-state">Las figuras todavía no están disponibles.</p>}
          </div>
        </div>
      </div>
    </section>
  );
}

function statusLabel(status: string): string {
  return (
    {
      queued: "En cola",
      validating: "Validando entrada",
      running: "Análisis en curso",
      cancelling: "Cancelación solicitada",
      cancelled: "Cancelado",
      completed: "Completado",
      partial: "Completado con limitaciones",
      failed: "Falló el análisis",
    }[status] ?? status
  );
}

function stageLabel(stage: string): string {
  return humanize(stage);
}

function formatArea(value: number): string {
  return `${value.toLocaleString("es-AR", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ha`;
}

function formatValue(value: unknown): string {
  if (typeof value === "number") return value.toLocaleString("es-AR", { maximumFractionDigits: 2 });
  if (typeof value === "string") return value;
  if (typeof value === "boolean") return value ? "Sí" : "No";
  return "—";
}

function humanize(value: string): string {
  const spaced = value.replaceAll("_", " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

function interpretationLabel(value: string): string {
  const labels: Record<string, string> = {
    candidate_only: "Sólo candidata",
    temporary_or_recovered: "Temporal o recuperada",
    persistent_unattributed: "Persistente sin atribución",
    insufficient_data: "Datos insuficientes",
    subthreshold_conversion_evidence: "Evidencia conjunta bajo umbral; no es evento",
    conversion_likely: "Conversión probable",
  };
  return labels[value] ?? humanize(value);
}

function resultArea(record: Record<string, unknown>, kind: "event" | "candidate"): number | null {
  const preferred =
    kind === "event"
      ? record.likely_conversion_area_ha
      : record.candidate_area_ha ?? record.area_ha;
  return typeof preferred === "number" ? preferred : null;
}

function geometryFeedbackFor({
  accepted,
  hasDraft,
  validating,
}: {
  accepted: boolean;
  hasDraft: boolean;
  validating: boolean;
}): { kind: "empty" | "pending" | "validating" | "accepted"; title: string; detail: string } {
  if (validating) {
    return {
      kind: "validating",
      title: "Validando geometría…",
      detail: "Esperá la confirmación antes de crear el análisis.",
    };
  }
  if (accepted) {
    return {
      kind: "accepted",
      title: "Geometría aceptada y validada",
      detail: "Esta es la geometría que se enviará al crear el análisis.",
    };
  }
  if (hasDraft) {
    return {
      kind: "pending",
      title: "Geometría pendiente de aceptación",
      detail: "Revisala en el mapa. Si volviste a editar, tenés que aceptarla nuevamente.",
    };
  }
  return {
    kind: "empty",
    title: "Aún no hay geometría",
    detail: "Cargá un GeoJSON, dibujá un polígono o arrastrá un rectángulo sobre el mapa.",
  };
}

function mutationError(error: unknown): string | null {
  if (!error) return null;
  if (error instanceof ApiError) {
    return [error.message, ...error.details].join(" · ");
  }
  return messageFrom(error);
}

function messageFrom(error: unknown): string {
  return error instanceof Error ? error.message : "Ocurrió un error inesperado";
}

function recordValue(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function analysisIdFromLocation(): string | null {
  const candidate = new URLSearchParams(window.location.search).get("analysis");
  return candidate && UUID_PATTERN.test(candidate) ? candidate : null;
}
