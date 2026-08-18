import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import type {
  PatientCreateRequest,
  PatientDetail,
  PatientListItem,
  ReviewRequest,
  ScanRead,
  Stage,
  SyntheticScanRequest,
} from "@/types/api";

export interface PatientListFilters {
  stage?: Stage;
  needsReview?: boolean;
  sortByUncertainty?: boolean;
}

function buildQuery(params: PatientListFilters): string {
  const search = new URLSearchParams();
  if (params.stage !== undefined) search.set("stage", params.stage);
  if (params.needsReview !== undefined) search.set("needsReview", String(params.needsReview));
  if (params.sortByUncertainty !== undefined) {
    search.set("sortByUncertainty", String(params.sortByUncertainty));
  }
  const qs = search.toString();
  return qs ? `?${qs}` : "";
}

export const listPatients = (filters: PatientListFilters = {}) =>
  api.get<PatientListItem[]>(`/patients${buildQuery(filters)}`);

export const getPatient = (id: string) => api.get<PatientDetail>(`/patients/${id}`);

export const createPatient = (payload: PatientCreateRequest) =>
  api.post<PatientDetail>("/patients", payload);

export const createSyntheticScan = (patientId: string, payload: SyntheticScanRequest) =>
  api.post<ScanRead>(`/patients/${patientId}/scans/synthetic`, payload);

export const uploadScan = (patientId: string, file: File) => {
  const form = new FormData();
  form.append("file", file);
  return api.postForm<ScanRead>(`/patients/${patientId}/scans/upload`, form);
};

export const reviewScan = (patientId: string, scanId: string, payload: ReviewRequest) =>
  api.post<ScanRead>(`/patients/${patientId}/scans/${scanId}/review`, payload);

const patientsKey = (filters?: PatientListFilters) => ["patients", filters] as const;
const patientKey = (id: string) => ["patients", id] as const;

export function usePatients(filters: PatientListFilters = {}) {
  return useQuery({
    queryKey: patientsKey(filters),
    queryFn: () => listPatients(filters),
    refetchInterval: 15_000, // cheap way to surface scans that finish processing in the background
  });
}

export function usePatient(id: string | undefined) {
  return useQuery({
    queryKey: patientKey(id ?? ""),
    queryFn: () => getPatient(id!),
    enabled: Boolean(id),
    refetchInterval: (query) => {
      // Keep polling while any scan is still mid-pipeline; stop once settled.
      const scans = query.state.data?.scans ?? [];
      const stillProcessing = scans.some(
        (s) => s.status !== "completed" && s.status !== "failed"
      );
      return stillProcessing ? 3_000 : false;
    },
  });
}

export function useCreatePatient() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createPatient,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["patients"] });
      queryClient.invalidateQueries({ queryKey: ["cohort"] });
    },
  });
}

export function useCreateSyntheticScan(patientId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: SyntheticScanRequest) => createSyntheticScan(patientId, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: patientKey(patientId) });
      queryClient.invalidateQueries({ queryKey: ["patients"] });
      queryClient.invalidateQueries({ queryKey: ["cohort"] });
    },
  });
}

export function useUploadScan(patientId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (file: File) => uploadScan(patientId, file),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: patientKey(patientId) });
      queryClient.invalidateQueries({ queryKey: ["patients"] });
      queryClient.invalidateQueries({ queryKey: ["cohort"] });
    },
  });
}

export function useReviewScan(patientId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ scanId, reviewed }: { scanId: string; reviewed: boolean }) =>
      reviewScan(patientId, scanId, { reviewed }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: patientKey(patientId) });
      queryClient.invalidateQueries({ queryKey: ["patients"] });
    },
  });
}
