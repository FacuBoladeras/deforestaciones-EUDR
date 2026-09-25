import { describe, expect, it } from "vitest";

import { closePolygonRing, featureFromBounds, parsePolygonDocument } from "./geojson";

describe("parsePolygonDocument", () => {
  it("normaliza una geometría Polygon a Feature sin inventar propiedades", () => {
    const feature = parsePolygonDocument(
      JSON.stringify({
        type: "Polygon",
        coordinates: [
          [
            [-60.3, -32.1],
            [-60.2, -32.1],
            [-60.2, -32.0],
            [-60.3, -32.1],
          ],
        ],
      }),
    );

    expect(feature.type).toBe("Feature");
    expect(feature.properties).toEqual({});
    expect(feature.geometry.type).toBe("Polygon");
  });

  it("normaliza una FeatureCollection de una sola geometría poligonal", () => {
    const feature = parsePolygonDocument(
      JSON.stringify({
        type: "FeatureCollection",
        features: [
          {
            type: "Feature",
            properties: { source: "fixture" },
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
          },
        ],
      }),
    );

    expect(feature.properties).toEqual({ source: "fixture" });
    expect(feature.geometry.type).toBe("Polygon");
  });

  it("rechaza puntos y colecciones que no representan un único establecimiento", () => {
    expect(() =>
      parsePolygonDocument(JSON.stringify({ type: "Point", coordinates: [-60, -32] })),
    ).toThrow("Sólo se admite Polygon o MultiPolygon");
    expect(() =>
      parsePolygonDocument(JSON.stringify({ type: "FeatureCollection", features: [] })),
    ).toThrow("FeatureCollection debe contener exactamente una Feature");
    expect(() =>
      parsePolygonDocument(
        JSON.stringify({
          type: "FeatureCollection",
          features: [{ type: "Feature", properties: {}, geometry: {} }, {}],
        }),
      ),
    ).toThrow("FeatureCollection debe contener exactamente una Feature");
  });

  it("cierra un anillo dibujado sin mutar los vértices originales", () => {
    const vertices: [number, number][] = [
      [-60.3, -32.1],
      [-60.2, -32.1],
      [-60.2, -32.0],
    ];

    const ring = closePolygonRing(vertices);

    expect(ring).toEqual([...vertices, vertices[0]]);
    expect(vertices).toHaveLength(3);
  });

  it("crea un rectángulo cerrado desde esquinas arrastradas en cualquier dirección", () => {
    const feature = featureFromBounds([-60.2, -32.0], [-60.3, -32.1]);

    expect(feature.geometry).toEqual({
      type: "Polygon",
      coordinates: [
        [
          [-60.3, -32.1],
          [-60.2, -32.1],
          [-60.2, -32.0],
          [-60.3, -32.0],
          [-60.3, -32.1],
        ],
      ],
    });
  });

  it("rechaza un arrastre sin ancho o sin alto", () => {
    expect(() => featureFromBounds([-60.2, -32.0], [-60.2, -32.1])).toThrow(
      "El rectángulo debe tener ancho y alto",
    );
  });
});
