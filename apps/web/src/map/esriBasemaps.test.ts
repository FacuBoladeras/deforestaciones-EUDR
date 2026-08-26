import { describe, expect, it } from "vitest";

import { createEsriBasemapStyleUrl } from "./esriBasemaps";

describe("createEsriBasemapStyleUrl", () => {
  it.each([
    ["imagery", "arcgis/imagery"],
    ["osm", "open/osm-style"],
  ] as const)("crea la URL oficial para %s", (basemap, path) => {
    expect(createEsriBasemapStyleUrl(basemap, "api key")).toBe(
      `https://basemapstyles-api.arcgis.com/arcgis/rest/services/styles/v2/styles/${path}?token=api%20key`,
    );
  });

  it("rechaza una clave vacía para no iniciar un mapa incompleto", () => {
    expect(() => createEsriBasemapStyleUrl("imagery", "  ")).toThrow(
      "arcgis_api_key_missing",
    );
  });
});
