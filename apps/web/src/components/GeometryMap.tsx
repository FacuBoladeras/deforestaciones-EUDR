import { useEffect, useRef, useState } from "react";
import type { Feature, FeatureCollection } from "geojson";
import {
  LngLatBounds,
  Map as MapLibreMap,
  NavigationControl,
  type GeoJSONSource,
  type MapMouseEvent,
} from "maplibre-gl";

import {
  featureFromBounds,
  featureFromVertices,
  geometryPositions,
  type PolygonFeature,
  type Position,
} from "../domain/geojson";
import {
  createEsriBasemapStyleUrl,
  type EsriBasemap,
} from "../map/esriBasemaps";

export type GeometryStatus = "empty" | "draft" | "accepted";

interface GeometryMapProps {
  geometry: PolygonFeature | null;
  status: GeometryStatus;
  onEditingStart: () => void;
  onEditingCancel: () => void;
  onGeometryChange: (geometry: PolygonFeature) => void;
  onGeometryClear: () => void;
}

type DrawingMode = "idle" | "polygon" | "rectangle";

const EMPTY_COLLECTION: FeatureCollection = { type: "FeatureCollection", features: [] };

export function GeometryMap({
  geometry,
  status,
  onEditingStart,
  onEditingCancel,
  onGeometryChange,
  onGeometryClear,
}: GeometryMapProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const modeRef = useRef<DrawingMode>("idle");
  const verticesRef = useRef<Position[]>([]);
  const rectangleStartRef = useRef<Position | null>(null);
  const geometryRef = useRef(geometry);
  const callbacksRef = useRef({
    onEditingStart,
    onEditingCancel,
    onGeometryChange,
    onGeometryClear,
  });
  const [mode, setMode] = useState<DrawingMode>("idle");
  const [vertexCount, setVertexCount] = useState(0);
  const [basemap, setBasemap] = useState<EsriBasemap>("imagery");
  const [mapStatus, setMapStatus] = useState<"loading" | "ready" | "error">("loading");

  geometryRef.current = geometry;

  callbacksRef.current = {
    onEditingStart,
    onEditingCancel,
    onGeometryChange,
    onGeometryClear,
  };

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    const apiKey = import.meta.env.VITE_ARCGIS_API_KEY?.trim();
    if (!apiKey) {
      setMapStatus("error");
      return;
    }
    const map = new MapLibreMap({
      container: containerRef.current,
      style: createEsriBasemapStyleUrl("imagery", apiKey),
      center: [-63.5, -34.8],
      zoom: 4,
    });
    map.addControl(new NavigationControl({ showCompass: false }), "top-right");
    map.on("load", () => {
      addGeometryLayers(map);
      setMapStatus("ready");
    });
    map.on("click", (event) => addPolygonVertex(map, event));
    map.on("mousedown", (event) => startRectangleDrag(event));
    map.on("mousemove", (event) => previewRectangle(map, event));
    map.on("mouseup", (event) => finishRectangleDrag(map, event));
    mapRef.current = map;
    return () => {
      map.remove();
      mapRef.current = null;
    };
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const render = () => {
      const source = map.getSource("analysis-geometry") as GeoJSONSource | undefined;
      source?.setData(geometry ?? EMPTY_COLLECTION);
      if (geometry) fitGeometry(map, geometry);
    };
    if (map.getSource("analysis-geometry")) render();
    else map.once("load", render);
  }, [geometry]);

  const resetDrawing = (map: MapLibreMap | null) => {
    modeRef.current = "idle";
    verticesRef.current = [];
    rectangleStartRef.current = null;
    setMode("idle");
    setVertexCount(0);
    if (!map) return;
    map.dragPan.enable();
    map.getCanvas().style.cursor = "";
    updateDrawingSource(map, EMPTY_COLLECTION);
  };

  const startDrawing = (nextMode: Exclude<DrawingMode, "idle">) => {
    resetDrawing(mapRef.current);
    modeRef.current = nextMode;
    setMode(nextMode);
    callbacksRef.current.onEditingStart();
    const map = mapRef.current;
    if (map) {
      map.getCanvas().style.cursor = "crosshair";
      if (nextMode === "rectangle") map.dragPan.disable();
    }
  };

  const undoVertex = () => {
    verticesRef.current = verticesRef.current.slice(0, -1);
    setVertexCount(verticesRef.current.length);
    if (mapRef.current) updatePolygonDraft(mapRef.current, verticesRef.current);
  };

  const finishPolygon = () => {
    if (verticesRef.current.length < 3) return;
    const feature = featureFromVertices(verticesRef.current);
    resetDrawing(mapRef.current);
    callbacksRef.current.onGeometryChange(feature);
  };

  const clearGeometry = () => {
    resetDrawing(mapRef.current);
    callbacksRef.current.onGeometryClear();
  };

  const cancelDrawing = () => {
    resetDrawing(mapRef.current);
    callbacksRef.current.onEditingCancel();
  };

  const addPolygonVertex = (map: MapLibreMap, event: MapMouseEvent) => {
    if (modeRef.current !== "polygon") return;
    verticesRef.current = [...verticesRef.current, positionFrom(event)];
    setVertexCount(verticesRef.current.length);
    updatePolygonDraft(map, verticesRef.current);
  };

  const startRectangleDrag = (event: MapMouseEvent) => {
    if (modeRef.current !== "rectangle") return;
    rectangleStartRef.current = positionFrom(event);
  };

  const previewRectangle = (map: MapLibreMap, event: MapMouseEvent) => {
    const start = rectangleStartRef.current;
    if (modeRef.current !== "rectangle" || !start) return;
    const current = positionFrom(event);
    if (!hasRectangleArea(start, current)) return;
    updateDrawingSource(map, collectionFrom(featureFromBounds(start, current)));
  };

  const finishRectangleDrag = (map: MapLibreMap, event: MapMouseEvent) => {
    const start = rectangleStartRef.current;
    if (modeRef.current !== "rectangle" || !start) return;
    rectangleStartRef.current = null;
    const opposite = positionFrom(event);
    if (!hasRectangleArea(start, opposite)) {
      updateDrawingSource(map, EMPTY_COLLECTION);
      return;
    }
    const feature = featureFromBounds(start, opposite);
    resetDrawing(map);
    callbacksRef.current.onGeometryChange(feature);
  };

  const displayedStatus = mode === "idle" ? status : "editing";

  const changeBasemap = () => {
    const map = mapRef.current;
    const apiKey = import.meta.env.VITE_ARCGIS_API_KEY?.trim();
    if (!map || !apiKey || mode !== "idle") return;
    const nextBasemap: EsriBasemap = basemap === "imagery" ? "osm" : "imagery";
    map.once("style.load", () => {
      addGeometryLayers(map);
      const source = map.getSource("analysis-geometry") as GeoJSONSource | undefined;
      source?.setData(geometryRef.current ?? EMPTY_COLLECTION);
    });
    map.setStyle(createEsriBasemapStyleUrl(nextBasemap, apiKey));
    setBasemap(nextBasemap);
  };

  return (
    <section className="map-card" aria-labelledby="map-title">
      <div className="map-heading">
        <div>
          <p className="eyebrow">Geometría</p>
          <h2 id="map-title">Ubicación del establecimiento</h2>
          <span className={`geometry-status geometry-status-${displayedStatus}`}>
            {statusLabel(status, mode)}
          </span>
        </div>
        <div className="map-actions">
          <label className="basemap-switch">
            <span>Satélite</span>
            <input
              type="checkbox"
              role="switch"
              aria-label="Cambiar mapa base"
              checked={basemap === "osm"}
              disabled={mapStatus !== "ready" || mode !== "idle"}
              onChange={changeBasemap}
            />
            <span>OSM</span>
          </label>
          {mode === "idle" ? (
            <>
              <button
                type="button"
                className="button button-secondary"
                onClick={() => startDrawing("polygon")}
                disabled={mapStatus !== "ready"}
              >
                {geometry ? "Reemplazar por polígono" : "Dibujar polígono"}
              </button>
              <button
                type="button"
                className="button button-secondary"
                onClick={() => startDrawing("rectangle")}
                disabled={mapStatus !== "ready"}
              >
                {geometry ? "Reemplazar por rectángulo" : "Dibujar rectángulo"}
              </button>
              {geometry && (
                <button type="button" className="button button-ghost" onClick={clearGeometry}>
                  Borrar geometría
                </button>
              )}
            </>
          ) : (
            <>
              {mode === "polygon" && (
                <>
                  <button
                    type="button"
                    className="button button-ghost"
                    onClick={undoVertex}
                    disabled={vertexCount === 0}
                  >
                    Deshacer
                  </button>
                  <button
                    type="button"
                    className="button button-primary"
                    onClick={finishPolygon}
                    disabled={vertexCount < 3}
                  >
                    Cerrar polígono
                  </button>
                </>
              )}
              <button
                type="button"
                className="button button-ghost"
                onClick={cancelDrawing}
              >
                Cancelar edición
              </button>
            </>
          )}
        </div>
      </div>
      {mode === "polygon" && (
        <p className="map-instruction" role="status">
          Marcá al menos tres vértices y cerrá el polígono. Llevás {vertexCount}.
        </p>
      )}
      {mode === "rectangle" && (
        <p className="map-instruction" role="status">
          Mantené presionado y arrastrá sobre el mapa para crear el rectángulo.
        </p>
      )}
      <div ref={containerRef} className="map" aria-label="Mapa para dibujar el establecimiento" />
      {mapStatus === "error" ? (
        <p className="map-note map-error" role="alert">
          No se pudieron cargar los mapas de Esri. Configurá VITE_ARCGIS_API_KEY en
          apps/web/.env.local.
        </p>
      ) : (
        <p className="map-note">
          El dibujo o la carga quedan pendientes hasta que presiones “Aceptar geometría”. Mapa
          base: {basemap === "imagery" ? "Esri Satellite" : "Esri OpenStreetMap"} (contexto
          visual; no constituye evidencia científica).
        </p>
      )}
    </section>
  );
}

function addGeometryLayers(map: MapLibreMap) {
  map.addSource("analysis-geometry", { type: "geojson", data: EMPTY_COLLECTION });
  map.addSource("drawing-geometry", { type: "geojson", data: EMPTY_COLLECTION });
  map.addLayer({
    id: "analysis-fill",
    type: "fill",
    source: "analysis-geometry",
    paint: { "fill-color": "#45a36b", "fill-opacity": 0.26 },
  });
  map.addLayer({
    id: "analysis-outline",
    type: "line",
    source: "analysis-geometry",
    paint: { "line-color": "#123f2b", "line-width": 3 },
  });
  map.addLayer({
    id: "drawing-fill",
    type: "fill",
    source: "drawing-geometry",
    paint: { "fill-color": "#e58b2b", "fill-opacity": 0.2 },
  });
  map.addLayer({
    id: "drawing-line",
    type: "line",
    source: "drawing-geometry",
    paint: { "line-color": "#e58b2b", "line-width": 3, "line-dasharray": [2, 1] },
  });
  map.addLayer({
    id: "drawing-points",
    type: "circle",
    source: "drawing-geometry",
    paint: { "circle-color": "#e58b2b", "circle-radius": 5 },
  });
}

function updatePolygonDraft(map: MapLibreMap, vertices: Position[]) {
  const features: Feature[] = vertices.map((coordinates) => ({
    type: "Feature",
    properties: {},
    geometry: { type: "Point", coordinates },
  }));
  if (vertices.length > 1) {
    features.unshift({
      type: "Feature",
      properties: {},
      geometry: { type: "LineString", coordinates: vertices },
    });
  }
  updateDrawingSource(map, { type: "FeatureCollection", features });
}

function updateDrawingSource(map: MapLibreMap, collection: FeatureCollection) {
  const source = map.getSource("drawing-geometry") as GeoJSONSource | undefined;
  source?.setData(collection);
}

function collectionFrom(feature: PolygonFeature): FeatureCollection {
  return { type: "FeatureCollection", features: [feature] };
}

function positionFrom(event: MapMouseEvent): Position {
  return [event.lngLat.lng, event.lngLat.lat];
}

function hasRectangleArea(first: Position, second: Position): boolean {
  return first[0] !== second[0] && first[1] !== second[1];
}

function statusLabel(status: GeometryStatus, mode: DrawingMode): string {
  if (mode === "rectangle") return "Dibujando rectángulo";
  if (mode === "polygon") return "Dibujando polígono";
  if (status === "accepted") return "Geometría aceptada";
  if (status === "draft") return "Pendiente de aceptación";
  return "Sin geometría";
}

function fitGeometry(map: MapLibreMap, geometry: PolygonFeature) {
  const positions = geometryPositions(geometry);
  if (positions.length === 0) return;
  const bounds = positions.reduce(
    (current, position) => current.extend(position),
    new LngLatBounds(positions[0], positions[0]),
  );
  map.fitBounds(bounds, { padding: 60, maxZoom: 15, duration: 450 });
}
