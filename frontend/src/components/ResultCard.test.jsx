import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ResultCard from "./ResultCard";
import UploadPanel from "./UploadPanel";
import { apiService } from "../services/api";
import { SCAN_POLL_INTERVAL_MS } from "../utils/scan";

const baseScan = {
  id: "async-scan",
  filename: "recording.wav",
  size_bytes: 100,
  duration_seconds: 2,
  created_at: "2026-01-01T00:00:00Z",
  model_version: "research-v1",
  scam_status: "review",
  scam_score: 0.72,
  scam_categories: ["Urgency"],
  scam_explanation: "Language signal remains available while voice analysis runs.",
};

const flushPromises = () => act(async () => {
  await Promise.resolve();
  await Promise.resolve();
});

describe("async scan result lifecycle", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("starts polling an in-flight scan and stops after it completes", async () => {
    const getScan = vi.spyOn(apiService, "getScan")
      .mockResolvedValueOnce({ data: { scan: { id: baseScan.id, status: "processing" } } })
      .mockResolvedValueOnce({ data: { scan: { id: baseScan.id, status: "completed", deepfake_status: "high", confidence: 0.84 } } });

    render(<ResultCard scanId={baseScan.id} initialScan={{ ...baseScan, status: "pending" }} onClose={vi.fn()} onDelete={vi.fn()} />);
    await flushPromises();
    expect(getScan).toHaveBeenCalledTimes(1);
    expect(screen.getAllByText("Processing").length).toBeGreaterThan(0);

    await act(async () => {
      vi.advanceTimersByTime(SCAN_POLL_INTERVAL_MS);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(getScan).toHaveBeenCalledTimes(2);
    expect(screen.getByText("84%")).toBeInTheDocument();
    expect(screen.getByText("Urgency")).toBeInTheDocument();

    await act(async () => {
      vi.advanceTimersByTime(SCAN_POLL_INTERVAL_MS * 2);
      await Promise.resolve();
    });
    expect(getScan).toHaveBeenCalledTimes(2);
  });

  it.each(["completed", "failed", "unavailable"])("stops polling when the scan is %s", async (status) => {
    const getScan = vi.spyOn(apiService, "getScan").mockResolvedValue({ data: { scan: { ...baseScan, status } } });

    render(<ResultCard scanId={baseScan.id} initialScan={{ ...baseScan, status: "pending" }} onClose={vi.fn()} onDelete={vi.fn()} />);
    await flushPromises();
    expect(getScan).toHaveBeenCalledTimes(1);

    await act(async () => {
      vi.advanceTimersByTime(SCAN_POLL_INTERVAL_MS * 2);
      await Promise.resolve();
    });
    expect(getScan).toHaveBeenCalledTimes(1);
  });

  it("renders a safe failed state while preserving scam-language fields", () => {
    const rawTranscript = "This private transcript must never be rendered.";
    const getScan = vi.spyOn(apiService, "getScan");
    render(<ResultCard
      initialScan={{
        ...baseScan,
        status: "failed",
        error_code: "inference_failed",
        error_explanation: "The voice analysis could not be completed.",
        transcript: rawTranscript,
      }}
      onClose={vi.fn()}
      onDelete={vi.fn()}
    />);

    expect(getScan).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("Analysis failed");
    expect(screen.getByRole("alert")).toHaveTextContent("The voice analysis could not be completed.");
    expect(screen.getByText("Error code: inference_failed")).toBeInTheDocument();
    expect(screen.getByText("72%")).toBeInTheDocument();
    expect(screen.getByText("Urgency")).toBeInTheDocument();
    expect(screen.queryByText(rawTranscript)).not.toBeInTheDocument();
  });

  it("shows queued messaging for the asynchronous upload admission", async () => {
    vi.spyOn(apiService, "uploadScan").mockResolvedValue({ data: { scan: { ...baseScan, status: "pending" } } });
    render(<UploadPanel onUploaded={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("WAV audio file"), { target: { files: [new File(["RIFF"], "queued.wav", { type: "audio/wav" })] } });
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: /start review/i }));
    await flushPromises();

    expect(screen.getByRole("status")).toHaveTextContent(/queued for analysis/i);
    expect(screen.queryByText(/no model score/i)).not.toBeInTheDocument();
  });
});
