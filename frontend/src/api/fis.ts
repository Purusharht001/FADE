import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "@/api/client";
import type { BiomarkerDef, FISResult, FISSimulateRequest, RuleDescription } from "@/types/api";

export const listBiomarkerDefs = () => api.get<BiomarkerDef[]>("/fis/biomarkers");
export const listRules = () => api.get<RuleDescription[]>("/fis/rules");
export const simulateFIS = (payload: FISSimulateRequest) =>
  api.post<FISResult>("/fis/simulate", payload);

export function useBiomarkerDefs() {
  return useQuery({
    queryKey: ["fis", "biomarkers"],
    queryFn: listBiomarkerDefs,
    staleTime: Infinity, // rule/biomarker definitions don't change during a session
  });
}

export function useRules() {
  return useQuery({
    queryKey: ["fis", "rules"],
    queryFn: listRules,
    staleTime: Infinity,
  });
}

export function useSimulateFIS() {
  return useMutation({ mutationFn: simulateFIS });
}
