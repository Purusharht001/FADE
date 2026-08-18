import { useQuery } from "@tanstack/react-query";
import { api } from "@/api/client";
import type { CohortStats } from "@/types/api";

export const getCohortStats = () => api.get<CohortStats>("/cohort/stats");

export function useCohortStats() {
  return useQuery({
    queryKey: ["cohort", "stats"],
    queryFn: getCohortStats,
    refetchInterval: 15_000,
  });
}
