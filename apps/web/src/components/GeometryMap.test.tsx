import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PolygonFeature } from "../domain/geojson";
import { GeometryMap } from "./GeometryMap";

const mapState = vi.hoisted(() => ({
  constructorOptions: null as null | { style?: unknown },
  handlers: new Map<string, (event: { lngLat: { lng: number; lat: number } }) => void>(),
  sources: new Map<string, { setData: ReturnType<typeof vi.fn> }>(),
  disableDragPan: vi.fn(),
  enableDragPan: vi.fn(),
  loaded: true,
  loadFired: false,
  setStyles: [] as unknown[],
  styleLoadHandler: null as null | (() => void),
}));

vi.mock("maplibre-gl", () => {
  class MapMock {
    constructor(options: { style?: unknown }) {
      mapState.constructorOptions = options;
    }

    dragPan = {
      disable: mapState.disableDragPan,
      enable: mapState.enableDragPan,
    };

    addControl() {}

    addSource(id: string) {
      mapState.sources.set(id, { setData: vi.fn() });
    }

    addLayer() {}

    on(type: string, handler: (event: { lngLat: { lng: number; lat: number } }) => void) {
      mapState.handlers.set(type, handler);
      if (type === "load") {
        mapState.loadFired = true;
        handler({ lngLat: { lng: 0, lat: 0 } });
      }
    }

    once(type: string, handler: () => void) {
      if (type === "load" && !mapState.loadFired) handler();
      if (type === "style.load") mapState.styleLoadHandler = handler;
    }

    loaded() {
      return mapState.loaded;
    }

    getSource(id: string) {
      return mapState.sources.get(id);
    }

    getCanvas() {
      return { style: { cursor: "" } };
    }

    fitBounds() {}

    setStyle(style: unknown) {
      mapState.sources.clear();
      mapState.setStyles.push(style);
      mapState.styleLoadHandler?.();
      mapState.styleLoadHandler = null;
    }

    remove() {}
  }

  class LngLatBoundsMock {
    extend() {
      return this;
    }
  }

  return {
    Map: MapMock,
    NavigationControl: class {},
    LngLatBounds: LngLatBoundsMock,
  };
});

const existingGeometry: PolygonFeature = {
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

beforeEach(() => {
  vi.stubEnv("VITE_ARCGIS_API_KEY", "test-key");
});

afterEach(() => {
  cleanup();
  mapState.handlers.clear();
  mapState.sources.clear();
  mapState.disableDragPan.mockClear();
  mapState.enableDragPan.mockClear();
  mapState.loaded = true;
  mapState.loadFired = false;
  mapState.constructorOptions = null;
  mapState.setStyles = [];
  mapState.styleLoadHandler = null;
  vi.unstubAllEnvs();
});

describe("GeometryMap", () => {
  it("actualiza la geometría aunque haya teselas pendientes después del load inicial", async () => {
    const view = render(
      <GeometryMap
        geometry={null}
        status="empty"
        onEditingStart={vi.fn()}
        onEditingCancel={vi.fn()}
        onGeometryChange={vi.fn()}
        onGeometryClear={vi.fn()}
      />,
    );
    await waitFor(() => expect(mapState.loadFired).toBe(true));
    const source = mapState.sources.get("analysis-geometry");
    mapState.loaded = false;

    view.rerender(
      <GeometryMap
        geometry={existingGeometry}
        status="draft"
        onEditingStart={vi.fn()}
        onEditingCancel={vi.fn()}
        onGeometryChange={vi.fn()}
        onGeometryClear={vi.fn()}
      />,
    );

    expect(source?.setData).toHaveBeenCalledWith(existingGeometry);
  });

  it("crea un rectángulo por arrastre y restaura la navegación al terminar", async () => {
    const user = userEvent.setup();
    const onEditingStart = vi.fn();
    const onGeometryChange = vi.fn();
    render(
      <GeometryMap
        geometry={null}
        status="empty"
        onEditingStart={onEditingStart}
        onEditingCancel={vi.fn()}
        onGeometryChange={onGeometryChange}
        onGeometryClear={vi.fn()}
      />,
    );
    await waitFor(() => expect(mapState.loadFired).toBe(true));

    await user.click(screen.getByRole("button", { name: "Dibujar rectángulo" }));
    expect(onEditingStart).toHaveBeenCalledOnce();
    expect(mapState.disableDragPan).toHaveBeenCalledOnce();
    expect(screen.getByText("Dibujando rectángulo")).toBeInTheDocument();
    expect(screen.getByText(/mantené presionado y arrastrá/i)).toBeInTheDocument();

    act(() => {
      mapState.handlers.get("mousedown")?.({ lngLat: { lng: -60.2, lat: -32.0 } });
      mapState.handlers.get("mousemove")?.({ lngLat: { lng: -60.3, lat: -32.1 } });
      mapState.handlers.get("mouseup")?.({ lngLat: { lng: -60.3, lat: -32.1 } });
    });

    expect(onGeometryChange).toHaveBeenCalledWith(
      expect.objectContaining({
        geometry: {
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
        },
      }),
    );
    expect(mapState.enableDragPan).toHaveBeenCalled();
  });

  it("muestra el estado aceptado y permite reemplazar o borrar", async () => {
    const user = userEvent.setup();
    const onEditingStart = vi.fn();
    const onEditingCancel = vi.fn();
    const onGeometryClear = vi.fn();
    render(
      <GeometryMap
        geometry={existingGeometry}
        status="accepted"
        onEditingStart={onEditingStart}
        onEditingCancel={onEditingCancel}
        onGeometryChange={vi.fn()}
        onGeometryClear={onGeometryClear}
      />,
    );
    await waitFor(() => expect(mapState.loadFired).toBe(true));

    expect(screen.getByText("Geometría aceptada")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Reemplazar por polígono" }));
    expect(onEditingStart).toHaveBeenCalledOnce();
    await user.click(screen.getByRole("button", { name: "Cancelar edición" }));
    expect(onEditingCancel).toHaveBeenCalledOnce();
    await user.click(screen.getByRole("button", { name: "Borrar geometría" }));
    expect(onGeometryClear).toHaveBeenCalledOnce();
  });

  it("alterna entre Esri satelital y Esri OSM sin perder la geometría", async () => {
    const user = userEvent.setup();
    render(
      <GeometryMap
        geometry={existingGeometry}
        status="accepted"
        onEditingStart={vi.fn()}
        onEditingCancel={vi.fn()}
        onGeometryChange={vi.fn()}
        onGeometryClear={vi.fn()}
      />,
    );

    await waitFor(() => expect(mapState.loadFired).toBe(true));
    expect(mapState.constructorOptions?.style).toBe(
      "https://basemapstyles-api.arcgis.com/arcgis/rest/services/styles/v2/styles/arcgis/imagery?token=test-key",
    );

    const basemapSwitch = screen.getByRole("switch", { name: /mapa base/i });
    await user.click(basemapSwitch);

    expect(mapState.setStyles).toEqual([
      "https://basemapstyles-api.arcgis.com/arcgis/rest/services/styles/v2/styles/open/osm-style?token=test-key",
    ]);
    expect(basemapSwitch).toBeChecked();
    expect(mapState.sources.get("analysis-geometry")?.setData).toHaveBeenCalledWith(
      existingGeometry,
    );
    expect(screen.getByText(/Esri OpenStreetMap/)).toBeInTheDocument();
  });

  it("expone la configuración faltante y evita editar sin mapa", async () => {
    vi.stubEnv("VITE_ARCGIS_API_KEY", "");
    render(
      <GeometryMap
        geometry={null}
        status="empty"
        onEditingStart={vi.fn()}
        onEditingCancel={vi.fn()}
        onGeometryChange={vi.fn()}
        onGeometryClear={vi.fn()}
      />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent("VITE_ARCGIS_API_KEY");
    expect(screen.getByRole("button", { name: "Dibujar polígono" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Dibujar rectángulo" })).toBeDisabled();
  });
});
