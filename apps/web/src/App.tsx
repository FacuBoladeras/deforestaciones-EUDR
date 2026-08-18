import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

import { AnalysisWorkspace } from "./features/analysis/AnalysisWorkspace";

export function App() {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: { retry: 1, staleTime: 2_000 },
          mutations: { retry: false },
        },
      }),
  );
  return (
    <QueryClientProvider client={queryClient}>
      <AnalysisWorkspace />
    </QueryClientProvider>
  );
}
