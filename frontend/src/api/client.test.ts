import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "@/api/client";
import { useAuthStore } from "@/store/auth";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("api client error handling", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
    useAuthStore.setState({ accessToken: null, refreshToken: null, user: null, isAuthenticated: false });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("parses a domain error shape from app/core/exceptions.py", async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(422, { error: { code: "unprocessable_scan", message: "Not a gzipped file" } })
    );

    await expect(api.get("/patients/x")).rejects.toMatchObject({
      status: 422,
      code: "unprocessable_scan",
      message: "Not a gzipped file",
    });
  });

  it("flags 422 unprocessable_scan errors distinctly via isUnprocessableScan", async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(422, { error: { code: "unprocessable_scan", message: "bad scan" } })
    );

    try {
      await api.get("/patients/x");
      expect.fail("expected api.get to throw");
    } catch (err) {
      expect(err).toBeInstanceOf(ApiError);
      expect((err as ApiError).isUnprocessableScan).toBe(true);
    }
  });

  it("parses FastAPI's own request-validation error shape (string detail)", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(400, { detail: "Malformed request" }));

    await expect(api.get("/x")).rejects.toMatchObject({
      code: "validation_error",
      message: "Malformed request",
    });
  });

  it("parses FastAPI's pydantic validation error shape (array detail)", async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(422, {
        detail: [{ type: "greater_than_equal", loc: ["body", "severity"], msg: "Input should be >= 0" }],
      })
    );

    await expect(api.post("/fis/simulate", {})).rejects.toMatchObject({
      code: "validation_error",
      message: "severity: Input should be >= 0",
    });
  });

  it("falls back to statusText when the error body isn't JSON", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response("<html>502 Bad Gateway</html>", { status: 502, statusText: "Bad Gateway" })
    );

    await expect(api.get("/x")).rejects.toMatchObject({ status: 502, message: "Bad Gateway" });
  });

  it("logs the session out on a 401 so the route guard redirects to /login", async () => {
    useAuthStore.setState({
      accessToken: "stale-token",
      refreshToken: "stale-refresh",
      user: { id: "1", email: "a@b.com", fullName: "A", role: "clinician", isActive: true },
      isAuthenticated: true,
    });
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(401, { error: { code: "unauthorized", message: "Token expired" } })
    );

    await expect(api.get("/patients")).rejects.toThrow();
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(useAuthStore.getState().accessToken).toBeNull();
  });

  it("does not attempt a logout loop for unauthenticated requests (e.g. /auth/login itself)", async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(401, { error: { code: "unauthorized", message: "Incorrect email or password." } })
    );

    await expect(api.post("/auth/login", {}, { auth: false })).rejects.toThrow();
    // Nothing was logged in to begin with, and nothing should have changed.
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
  });

  it("sends a bearer token when authenticated", async () => {
    useAuthStore.setState({
      accessToken: "real-token",
      refreshToken: "r",
      user: null,
      isAuthenticated: true,
    });
    const mockFetch = vi.mocked(fetch);
    mockFetch.mockResolvedValue(jsonResponse(200, { ok: true }));

    await api.get("/patients");

    const headers = mockFetch.mock.calls[0][1]?.headers as Headers;
    expect(headers.get("Authorization")).toBe("Bearer real-token");
  });
});
