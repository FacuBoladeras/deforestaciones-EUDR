export type Position = [number, number];

export interface PolygonGeometry {
  type: "Polygon";
  coordinates: Position[][];
}

export interface MultiPolygonGeometry {
  type: "MultiPolygon";
  coordinates: Position[][][];
}

export type SupportedGeometry = PolygonGeometry | MultiPolygonGeometry;

export interface PolygonFeature {
  type: "Feature";
  properties: Record<string, unknown>;
  geometry: SupportedGeometry;
}

export function parsePolygonDocument(content: string): PolygonFeature {
  let document: unknown;
  try {
    document = JSON.parse(content);
  } catch {
    throw new Error("El archivo no contiene JSON válido");
  }
  if (!isRecord(document)) {
    throw new Error("GeoJSON debe ser un objeto");
  }
  const candidate =
    document.type === "FeatureCollection" ? singleFeatureFromCollection(document) : document;

  const geometry = candidate.type === "Feature" ? candidate.geometry : candidate;
  if (!isSupportedGeometry(geometry)) {
    throw new Error("Sólo se admite Polygon o MultiPolygon");
  }
  const properties =
    candidate.type === "Feature" && isRecord(candidate.properties) ? candidate.properties : {};
  return {
    type: "Feature",
    properties,
    geometry,
  };
}

function singleFeatureFromCollection(collection: Record<string, unknown>): Record<string, unknown> {
  const features = collection.features;
  if (
    !Array.isArray(features) ||
    features.length !== 1 ||
    !isRecord(features[0]) ||
    features[0].type !== "Feature"
  ) {
    throw new Error("FeatureCollection debe contener exactamente una Feature");
  }
  return features[0];
}

export function closePolygonRing(vertices: readonly Position[]): Position[] {
  if (vertices.length < 3) {
    throw new Error("Se necesitan al menos tres vértices");
  }
  const ring = vertices.map(([longitude, latitude]) => [longitude, latitude] as Position);
  const first = ring[0];
  const last = ring.at(-1);
  if (!first || !last) {
    throw new Error("El anillo está vacío");
  }
  if (first[0] !== last[0] || first[1] !== last[1]) {
    ring.push([...first]);
  }
  return ring;
}

export function featureFromVertices(vertices: readonly Position[]): PolygonFeature {
  return {
    type: "Feature",
    properties: {},
    geometry: { type: "Polygon", coordinates: [closePolygonRing(vertices)] },
  };
}

export function featureFromBounds(first: Position, opposite: Position): PolygonFeature {
  const west = Math.min(first[0], opposite[0]);
  const east = Math.max(first[0], opposite[0]);
  const south = Math.min(first[1], opposite[1]);
  const north = Math.max(first[1], opposite[1]);
  if (west === east || south === north) {
    throw new Error("El rectángulo debe tener ancho y alto");
  }
  return featureFromVertices([
    [west, south],
    [east, south],
    [east, north],
    [west, north],
  ]);
}

export function geometryPositions(feature: PolygonFeature): Position[] {
  if (feature.geometry.type === "Polygon") {
    return feature.geometry.coordinates.flat();
  }
  return feature.geometry.coordinates.flat(2);
}

function isSupportedGeometry(value: unknown): value is SupportedGeometry {
  if (!isRecord(value) || (value.type !== "Polygon" && value.type !== "MultiPolygon")) {
    return false;
  }
  return value.type === "Polygon"
    ? isPolygonCoordinates(value.coordinates)
    : isMultiPolygonCoordinates(value.coordinates);
}

function isMultiPolygonCoordinates(value: unknown): value is Position[][][] {
  return Array.isArray(value) && value.length > 0 && value.every(isPolygonCoordinates);
}

function isPolygonCoordinates(value: unknown): value is Position[][] {
  return Array.isArray(value) && value.length > 0 && value.every(isRing);
}

function isRing(value: unknown): value is Position[] {
  return Array.isArray(value) && value.length >= 4 && value.every(isPosition);
}

function isPosition(value: unknown): value is Position {
  return (
    Array.isArray(value) &&
    value.length >= 2 &&
    typeof value[0] === "number" &&
    Number.isFinite(value[0]) &&
    typeof value[1] === "number" &&
    Number.isFinite(value[1])
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
