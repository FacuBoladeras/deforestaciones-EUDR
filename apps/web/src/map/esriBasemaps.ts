export type EsriBasemap = "imagery" | "osm";

const STYLE_ENDPOINT =
  "https://basemapstyles-api.arcgis.com/arcgis/rest/services/styles/v2/styles";

const STYLE_PATHS: Record<EsriBasemap, string> = {
  imagery: "arcgis/imagery",
  osm: "open/osm-style",
};

export function createEsriBasemapStyleUrl(basemap: EsriBasemap, apiKey: string): string {
  const normalizedKey = apiKey.trim();
  if (!normalizedKey) throw new Error("arcgis_api_key_missing");
  return `${STYLE_ENDPOINT}/${STYLE_PATHS[basemap]}?token=${encodeURIComponent(normalizedKey)}`;
}
