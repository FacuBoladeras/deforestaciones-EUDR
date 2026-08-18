import { describe, expect, it } from "vitest";

import config from "../../vite.config";

describe("Vite development configuration", () => {
  it("excluye MapLibre del prebundle que rompe la carga de su worker", () => {
    expect(config).toMatchObject({
      optimizeDeps: { exclude: ["maplibre-gl"] },
    });
  });
});
