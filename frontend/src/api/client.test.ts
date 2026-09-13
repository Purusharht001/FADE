import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "@/api/client";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("api client error handling", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
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

  it("does not attach an Authorization header (no login flow is wired up)", async () => {
    const mockFetch = vi.mocked(fetch);
    mockFetch.mockResolvedValue(jsonResponse(200, { ok: true }));

    await api.get("/patients");

    const headers = mockFetch.mock.calls[0][1]?.headers as Headers;
    expect(headers.get("Authorization")).toBeNull();
  });
});
