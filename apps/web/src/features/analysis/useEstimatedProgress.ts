import { useEffect, useState } from "react";

import type { AnalysisStatus } from "../../api/client";

type Status = AnalysisStatus["status"];

const ACTIVE_PROGRESS: Partial<
  Record<Status, { floor: number; ceiling: number; intervalMs: number }>
> = {
  queued: { floor: 6, ceiling: 12, intervalMs: 2_000 },
  validating: { floor: 16, ceiling: 24, intervalMs: 5_000 },
  running: { floor: 28, ceiling: 92, intervalMs: 60_000 },
};

export function useEstimatedProgress(status: Status): number {
  const [progress, setProgress] = useState(() => initialProgress(status));

  useEffect(() => {
    if (status === "completed" || status === "partial") {
      setProgress(100);
      return;
    }
    const range = ACTIVE_PROGRESS[status];
    if (range) {
      setProgress((current) => Math.max(current, range.floor));
    }
  }, [status]);

  useEffect(() => {
    const range = ACTIVE_PROGRESS[status];
    if (!range) return;
    const interval = window.setInterval(() => {
      setProgress((current) => Math.min(range.ceiling, current + 1));
    }, range.intervalMs);
    return () => window.clearInterval(interval);
  }, [status]);

  return progress;
}

function initialProgress(status: Status): number {
  if (status === "completed" || status === "partial") return 100;
  return ACTIVE_PROGRESS[status]?.floor ?? 0;
}
