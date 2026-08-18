import { useEffect, useRef, useState } from "react";
import type { Feature, FeatureCollection } from "geojson";
import {
  LngLatBounds,
  Map as MapLibreMap,
  NavigationControl,
  type GeoJSONSource,
  type StyleSpecification,
} from "maplibre-gl";

import {
  featureFromVertices,
  geometryPositions,
  type PolygonFeature,
  type Position,
} from "../domain/geojson";

interface GeometryMapProps {
  geometry: PolygonFeature | null;
  onGeometryChange: (geometry: PolygonFeature) => void;
}

const EMPTY_COLLECTION: FeatureCollection = {
  type: "FeatureCollection",
  features: [],
};

const BASE_STYLE: StyleSpecification = {
  version: 8,
  sources: {
    openstreetmap: {
      type: "raster",
      tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
      tileSize: 256,
      attribution: "© OpenStreetMap contributors",
      maxzoom: 19,
    },
  },
  layers: [{ id: "openstreetmap", type: "raster", source: "openstreetmap" }],
};

export function GeometryMap({ geometry, onGeometryChange }: GeometryMapProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const drawingRef = useRef(false);
  const verticesRef = useRef<Position[]>([]);
  const callbackRef = useRef(onGeometryChange);
  const [drawing, setDrawing] = useState(false);
  const [vertexCount, setVertexCount] = useState(0);

  callbackRef.current = onGeometryChange;

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    const map = new MapLibreMap({
      container: containerRef.current,
      style: BASE_STYLE,
      center: [-63.5, -34.8],
      zoom: 4,
    });
    map.addControl(new NavigationControl({ showCompass: false }), "top-right");
    map.on("load", () => {
      map.addSource("analysis-geometry", { type: "geojson", data: EMPTY_COLLECTION });
      map.addSource("drawing-vertices", { type: "geojson", data: EMPTY_COLLECTION });
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
        id: "drawing-line",
        type: "line",
        source: "drawing-vertices",
        paint: { "line-color": "#e58b2b", "line-width": 3, "line-dasharray": [2, 1] },
      });
      map.addLayer({
        id: "drawing-points",
        type: "circle",
        source: "drawing-vertices",
        paint: { "circle-color": "#e58b2b", "circle-radius": 5 },
      });
    });
    map.on("click", ({ lngLat }) => {
      if (!drawingRef.current) return;
      verticesRef.current = [...verticesRef.current, [lngLat.lng, lngLat.lat]];
      setVertexCount(verticesRef.current.length);
      updateDraft(map, verticesRef.current);
    });
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
    if (map.loaded()) render();
    else map.once("load", render);
  }, [geometry]);

  const startDrawing = () => {
    verticesRef.current = [];
    drawingRef.current = true;
    setDrawing(true);
    setVertexCount(0);
    if (mapRef.current) updateDraft(mapRef.current, []);
  };

  const undoVertex = () => {
    verticesRef.current = verticesRef.current.slice(0, -1);
    setVertexCount(verticesRef.current.length);
    if (mapRef.current) updateDraft(mapRef.current, verticesRef.current);
  };

  const finishDrawing = () => {
    if (verticesRef.current.length < 3) return;
    const feature = featureFromVertices(verticesRef.current);
    drawingRef.current = false;
    setDrawing(false);
    setVertexCount(0);
    verticesRef.current = [];
    if (mapRef.current) updateDraft(mapRef.current, []);
    callbackRef.current(feature);
  };

  return (
    <section className="map-card" aria-labelledby="map-title">
      <div className="map-heading">
        <div>
          <p className="eyebrow">Geometría</p>
          <h2 id="map-title">Ubicación del establecimiento</h2>
        </div>
        <div className="map-actions">
          <button type="button" className="button button-secondary" onClick={startDrawing}>
            Dibujar polígono
          </button>
          {drawing && (
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
                onClick={finishDrawing}
                disabled={vertexCount < 3}
              >
                Cerrar polígono
              </button>
            </>
          )}
        </div>
      </div>
      {drawing && (
        <p className="map-instruction">Marcá al menos tres vértices. Llevás {vertexCount}.</p>
      )}
      <div ref={containerRef} className="map" aria-label="Mapa para dibujar el establecimiento" />
      <p className="map-note">
        El mapa base usa teselas públicas de OpenStreetMap; la geometría se valida contra la API
        antes de crear el análisis.
      </p>
    </section>
  );
}

function updateDraft(map: MapLibreMap, vertices: Position[]) {
  const source = map.getSource("drawing-vertices") as GeoJSONSource | undefined;
  if (!source) return;
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
  source.setData({ type: "FeatureCollection", features });
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
