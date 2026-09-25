import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AnalysisStatus } from "../../api/client";
import { useEstimatedProgress } from "./useEstimatedProgress";

type Status = AnalysisStatus["status"];

afterEach(() => vi.useRealTimers());

describe("useEstimatedProgress", () => {
  it("avanza artificialmente pero reserva el 100% para un resultado confirmado", () => {
    vi.useFakeTimers();
    const { result, rerender } = renderHook(
      ({ status }: { status: Status }) => useEstimatedProgress(status),
      { initialProps: { status: "queued" } },
    );

    expect(result.current).toBe(6);
    act(() => vi.advanceTimersByTime(4_000));
    expect(result.current).toBe(8);

    rerender({ status: "running" });
    expect(result.current).toBe(28);
    act(() => vi.advanceTimersByTime(180_000));
    expect(result.current).toBe(31);

    rerender({ status: "completed" });
    expect(result.current).toBe(100);
  });

  it("no inventa finalización cuando la API informa fallo", () => {
    vi.useFakeTimers();
    const { result, rerender } = renderHook(
      ({ status }: { status: Status }) => useEstimatedProgress(status),
      { initialProps: { status: "running" } },
    );

    act(() => vi.advanceTimersByTime(120_000));
    const beforeFailure = result.current;
    rerender({ status: "failed" });
    act(() => vi.advanceTimersByTime(10_000));

    expect(result.current).toBe(beforeFailure);
    expect(result.current).toBeLessThan(100);
  });
});
