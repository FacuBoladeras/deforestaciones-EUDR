import { describe, expect, it } from "vitest";

import packageManifest from "../../package.json";
import config from "../../vite.config";

describe("Vite development configuration", () => {
  it("excluye MapLibre del prebundle que rompe la carga de su worker", () => {
    expect(config).toMatchObject({
      optimizeDeps: { exclude: ["maplibre-gl"] },
    });
  });
});

describe("Vite production packaging contract", () => {
  it("typecheckea antes de generar el artefacto estático", () => {
    expect(packageManifest.scripts.build).toBe("tsc --noEmit && vite build");
  });

  it("genera un directorio determinístico con manifest y sin sourcemaps", () => {
    expect(config).toMatchObject({
      base: "/",
      build: {
        assetsDir: "assets",
        emptyOutDir: true,
        manifest: true,
        outDir: "dist",
        sourcemap: false,
        target: "es2022",
      },
    });
  });
});
