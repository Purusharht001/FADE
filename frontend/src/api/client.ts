import { getAccessToken, useAuthStore } from "@/store/auth";
import type { ApiErrorBody, ValidationErrorBody } from "@/types/api";

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";
const API_PREFIX = "/api/v1";

export class ApiError extends Error {
  status: number;
  code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }

  get isUnprocessableScan(): boolean {
    return this.status === 422 && this.code === "unprocessable_scan";
  }

  get isNotFound(): boolean {
    return this.status === 404;
  }
}

async function extractErrorMessage(response: Response): Promise<{ code: string; message: string }> {
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    return { code: "unknown", message: response.statusText || "Request failed" };
  }

  // Domain errors from app/core/exceptions.py: {"error": {"code", "message"}}
  const asApiError = body as Partial<ApiErrorBody>;
  if (asApiError?.error?.code && asApiError.error.message) {
    return { code: asApiError.error.code, message: asApiError.error.message };
  }

  // FastAPI/Pydantic request-validation errors: {"detail": "..." | [{msg, loc, ...}]}
  const asValidation = body as Partial<ValidationErrorBody>;
  if (typeof asValidation?.detail === "string") {
    return { code: "validation_error", message: asValidation.detail };
  }
  if (Array.isArray(asValidation?.detail) && asValidation.detail.length > 0) {
    const first = asValidation.detail[0];
    const field = first.loc?.at(-1);
    return {
      code: "validation_error",
      message: field ? `${field}: ${first.msg}` : first.msg,
    };
  }

  return { code: "unknown", message: response.statusText || "Request failed" };
}

interface RequestOptions extends Omit<RequestInit, "body"> {
  body?: unknown;
  /** Send as multipart/form-data instead of JSON. */
  form?: FormData;
  auth?: boolean;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { body, form, auth = true, headers, ...rest } = options;

  const finalHeaders = new Headers(headers);
  if (!form) finalHeaders.set("Content-Type", "application/json");

  if (auth) {
    const token = getAccessToken();
    if (token) finalHeaders.set("Authorization", `Bearer ${token}`);
  }

  const response = await fetch(`${BASE_URL}${API_PREFIX}${path}`, {
    ...rest,
    headers: finalHeaders,
    body: form ?? (body !== undefined ? JSON.stringify(body) : undefined),
  });

  if (!response.ok) {
    const { code, message } = await extractErrorMessage(response);
    if (response.status === 401 && auth) {
      // Session is invalid/expired — clear it so the route guard redirects
      // to /login instead of every subsequent call failing the same way.
      useAuthStore.getState().logout();
    }
    throw new ApiError(response.status, code, message);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  get: <T>(path: string, options?: RequestOptions) => request<T>(path, { ...options, method: "GET" }),
  post: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    request<T>(path, { ...options, method: "POST", body }),
  postForm: <T>(path: string, form: FormData, options?: RequestOptions) =>
    request<T>(path, { ...options, method: "POST", form }),
};
