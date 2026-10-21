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
    fireEvent.change(screen.getByLabelText("WAV or MP3 audio file"), { target: { files: [new File(["RIFF"], "queued.wav", { type: "audio/wav" })] } });
    fireEvent.click(screen.getByLabelText(/consent to submit this recording/i));
    fireEvent.click(screen.getByRole("button", { name: /start review/i }));
    await flushPromises();

    expect(screen.getByRole("status")).toHaveTextContent(/queued for analysis/i);
    expect(screen.queryByText(/no model score/i)).not.toBeInTheDocument();
  });

  it("shows retained playback with an owner-protected audio player", () => {
    render(<ResultCard
      initialScan={{
        ...baseScan,
        status: "completed",
        audio_retained: true,
        audio_playback_url: "https://media.example.test/owner-scan.wav",
      }}
      onClose={vi.fn()}
      onDelete={vi.fn()}
    />);

    expect(screen.getByRole("heading", { name: "Playback available" })).toBeInTheDocument();
    expect(screen.getByLabelText("Private audio playback")).toHaveAttribute("src", "https://media.example.test/owner-scan.wav");
    expect(screen.getByText("Retained for 30 days")).toBeInTheDocument();
  });

  it("shows explicit unretained playback availability without an audio player", () => {
    render(<ResultCard
      initialScan={{ ...baseScan, status: "completed", audio_retained: false }}
      onClose={vi.fn()}
      onDelete={vi.fn()}
    />);

    expect(screen.getByRole("heading", { name: "Playback not retained" })).toBeInTheDocument();
    expect(screen.getByText("Not retained")).toBeInTheDocument();
    expect(screen.queryByLabelText("Private audio playback")).not.toBeInTheDocument();
  });

  it("loads retained audio through the authenticated apiService endpoint", async () => {
    const originalCreateObjectURL = URL.createObjectURL;
    const originalRevokeObjectURL = URL.revokeObjectURL;
    const createObjectURL = vi.fn(() => "blob:owner-playback");
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, "createObjectURL", { configurable: true, writable: true, value: createObjectURL });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, writable: true, value: revokeObjectURL });
    const getScanPlayback = vi.spyOn(apiService, "getScanPlayback").mockResolvedValue({ data: new Blob(["RIFF"], { type: "audio/wav" }) });

    render(<ResultCard
      initialScan={{ ...baseScan, id: "retained-endpoint", status: "completed", audio_retained: true }}
      onClose={vi.fn()}
      onDelete={vi.fn()}
    />);
    await flushPromises();

    expect(getScanPlayback).toHaveBeenCalledWith("retained-endpoint", "/scans/retained-endpoint/audio");
    expect(screen.getByLabelText("Private audio playback")).toHaveAttribute("src", "blob:owner-playback");
    expect(createObjectURL).toHaveBeenCalled();

    Object.defineProperty(URL, "createObjectURL", { configurable: true, writable: true, value: originalCreateObjectURL });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, writable: true, value: originalRevokeObjectURL });
  });

  it("retries a transient playback network failure", async () => {
    const originalCreateObjectURL = URL.createObjectURL;
    const createObjectURL = vi.fn(() => "blob:retried-playback");
    Object.defineProperty(URL, "createObjectURL", { configurable: true, writable: true, value: createObjectURL });
    const getScanPlayback = vi.spyOn(apiService, "getScanPlayback")
      .mockRejectedValueOnce(Object.assign(new Error("Network Error"), { code: "ERR_NETWORK" }))
      .mockResolvedValueOnce({ data: new Blob(["RIFF"], { type: "audio/wav" }) });

    render(<ResultCard
      initialScan={{ ...baseScan, id: "retry-playback", status: "completed", audio_retained: true }}
      onClose={vi.fn()}
      onDelete={vi.fn()}
    />);
    await flushPromises();
    expect(getScanPlayback).toHaveBeenCalledTimes(1);

    await act(async () => {
      vi.advanceTimersByTime(1000);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(getScanPlayback).toHaveBeenCalledTimes(2);
    expect(screen.getByLabelText("Private audio playback")).toHaveAttribute("src", "blob:retried-playback");

    Object.defineProperty(URL, "createObjectURL", { configurable: true, writable: true, value: originalCreateObjectURL });
  });
});
