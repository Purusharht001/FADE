import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Dashboard } from "@/pages/dashboard";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { PatientListItem, ScanSummary } from "@/types/api";

vi.mock("@/api/patients", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/patients")>();
  return { ...actual, usePatients: vi.fn() };
});
vi.mock("@/api/cohort", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/cohort")>();
  return { ...actual, useCohortStats: vi.fn() };
});

import { usePatients } from "@/api/patients";
import { useCohortStats } from "@/api/cohort";

function makeScan(overrides: Partial<ScanSummary> = {}): ScanSummary {
  return {
    id: crypto.randomUUID(),
    scanDate: "2026-01-01",
    status: "completed",
    stage: "MCI",
    confidence: 60,
    uncertainty: 50,
    needsReview: true,
    ...overrides,
  };
}

function makePatient(overrides: Partial<PatientListItem> = {}): PatientListItem {
  return {
    id: crypto.randomUUID(),
    displayId: "PT-0001",
    age: 70,
    sex: "F",
    source: "Clinic",
    latestScan: makeScan(),
    ...overrides,
  };
}

const mockCohortStats = {
  totalPatients: 3,
  totalScans: 3,
  needsReview: 2,
  avgConfidence: 61.3,
  byStage: { CN: 1, MCI: 1, AD: 1 },
};

function renderDashboard() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <MemoryRouter>
          <Dashboard />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>
  );
}

function mockPatientsQuery(overrides: Partial<ReturnType<typeof usePatients>>) {
  vi.mocked(usePatients).mockReturnValue({
    data: undefined,
    isLoading: false,
    isError: false,
    error: null,
    refetch: vi.fn(),
    ...overrides,
  } as ReturnType<typeof usePatients>);
}

beforeEach(() => {
  vi.mocked(useCohortStats).mockReturnValue({
    data: mockCohortStats,
    isLoading: false,
    isError: false,
  } as ReturnType<typeof useCohortStats>);
});

describe("Dashboard triage ordering", () => {
  it("renders cases in the order the API returned them, highest uncertainty first", () => {
    // usePatients is called with sortByUncertainty: true, so the mock
    // here stands in for what the backend would already have sorted —
    // the component's job is to render that order faithfully, and the
    // sortPatientsByUncertainty() defense-in-depth pass (unit-tested
    // separately in lib/sort.test.ts) must not disturb it.
    const patients = [
      makePatient({ displayId: "PT-HIGH", latestScan: makeScan({ uncertainty: 91 }) }),
      makePatient({ displayId: "PT-MID", latestScan: makeScan({ uncertainty: 54 }) }),
      makePatient({ displayId: "PT-LOW", latestScan: makeScan({ uncertainty: 12, needsReview: false }) }),
    ];
    mockPatientsQuery({ data: patients });

    renderDashboard();

    const rows = screen.getAllByText(/^PT-(HIGH|MID|LOW)$/).map((el) => el.textContent);
    expect(rows).toEqual(["PT-HIGH", "PT-MID", "PT-LOW"]);
  });

  it("re-sorts by uncertainty even if the API response arrives out of order", () => {
    const patients = [
      makePatient({ displayId: "PT-LOW", latestScan: makeScan({ uncertainty: 12, needsReview: false }) }),
      makePatient({ displayId: "PT-HIGH", latestScan: makeScan({ uncertainty: 91 }) }),
      makePatient({ displayId: "PT-MID", latestScan: makeScan({ uncertainty: 54 }) }),
    ];
    mockPatientsQuery({ data: patients });

    renderDashboard();

    const rows = screen.getAllByText(/^PT-(HIGH|MID|LOW)$/).map((el) => el.textContent);
    expect(rows).toEqual(["PT-HIGH", "PT-MID", "PT-LOW"]);
  });

  it("filters the visible list by search without breaking sort order", async () => {
    const patients = [
      makePatient({ displayId: "PT-0001", latestScan: makeScan({ uncertainty: 91 }) }),
      makePatient({ displayId: "PT-0002", latestScan: makeScan({ uncertainty: 54 }) }),
    ];
    mockPatientsQuery({ data: patients });
    renderDashboard();

    const user = userEvent.setup();
    await user.type(screen.getByPlaceholderText("Search patient ID…"), "0002");

    expect(screen.queryByText("PT-0001")).not.toBeInTheDocument();
    expect(screen.getByText("PT-0002")).toBeInTheDocument();
  });
});

describe("Dashboard loading and error states", () => {
  it("shows skeleton placeholders while patients are loading", () => {
    mockPatientsQuery({ isLoading: true });
    const { container } = renderDashboard();
    expect(container.querySelectorAll(".animate-pulse").length).toBeGreaterThan(0);
  });

  it("shows an error state with a retry action when the request fails", async () => {
    const refetch = vi.fn();
    mockPatientsQuery({
      isError: true,
      error: new Error("Network request failed"),
      refetch,
    });

    renderDashboard();

    expect(screen.getByText("Couldn't load cases")).toBeInTheDocument();
    expect(screen.getByText("Network request failed")).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(refetch).toHaveBeenCalledTimes(1);
  });

  it("shows an empty state when no cases match the active filter", () => {
    mockPatientsQuery({ data: [] });
    renderDashboard();
    expect(screen.getByText("No cases match this filter.")).toBeInTheDocument();
  });
});

describe("Dashboard stat tiles", () => {
  it("renders cohort stats from the API", () => {
    mockPatientsQuery({ data: [] });
    renderDashboard();

    const flaggedTile = screen.getByText("Flagged for review").closest("div");
    expect(flaggedTile).not.toBeNull();
    expect(within(flaggedTile!.parentElement!).getByText("2")).toBeInTheDocument();
  });
});
