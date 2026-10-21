import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import UploadPanel from "./UploadPanel";
import { apiService } from "../services/api";

const originalCreateObjectURL = URL.createObjectURL;
const originalRevokeObjectURL = URL.revokeObjectURL;

describe("UploadPanel browser preview and retention controls", () => {
  let createObjectURL;
  let revokeObjectURL;

  beforeEach(() => {
    createObjectURL = vi.fn((file) => `blob:${file.name}`);
    revokeObjectURL = vi.fn();
    Object.defineProperty(URL, "createObjectURL", { configurable: true, writable: true, value: createObjectURL });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, writable: true, value: revokeObjectURL });
  });

  afterEach(() => {
    if (originalCreateObjectURL) Object.defineProperty(URL, "createObjectURL", { configurable: true, writable: true, value: originalCreateObjectURL });
    else delete URL.createObjectURL;
    if (originalRevokeObjectURL) Object.defineProperty(URL, "revokeObjectURL", { configurable: true, writable: true, value: originalRevokeObjectURL });
    else delete URL.revokeObjectURL;
    vi.restoreAllMocks();
  });

  it("creates an immediate browser-only audio preview without a network call", () => {
    render(<UploadPanel onUploaded={vi.fn()} />);
    const file = new File(["RIFF"], "preview.wav", { type: "audio/wav" });

    fireEvent.change(screen.getByLabelText("WAV or MP3 audio file"), { target: { files: [file] } });

    expect(createObjectURL).toHaveBeenCalledWith(file);
    expect(screen.getByLabelText("Audio recording preview")).toHaveAttribute("src", "blob:preview.wav");
    expect(screen.getByText("Nothing is uploaded by previewing")).toBeInTheDocument();
  });

  it("revokes the previous object URL when the selected file changes and on unmount", () => {
    const { unmount } = render(<UploadPanel onUploaded={vi.fn()} />);
    const input = screen.getByLabelText("WAV or MP3 audio file");
    const first = new File(["RIFF"], "first.wav", { type: "audio/wav" });
    const second = new File(["RIFF"], "second.wav", { type: "audio/wav" });

    fireEvent.change(input, { target: { files: [first] } });
    fireEvent.change(input, { target: { files: [second] } });
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:first.wav");

    unmount();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:second.wav");
  });

  it("sends the retention checkbox as retain_audio=true", async () => {
    const upload = vi.spyOn(apiService, "uploadScan").mockResolvedValue({ data: { scan: { id: "retained-scan", status: "pending" } } });
    render(<UploadPanel onUploaded={vi.fn()} />);
    const file = new File(["RIFF"], "retained.wav", { type: "audio/wav" });

    fireEvent.change(screen.getByLabelText("WAV or MP3 audio file"), { target: { files: [file] } });
    fireEvent.click(screen.getByLabelText(/consent to submit this recording/i));
    fireEvent.click(screen.getByLabelText("Keep this recording available for playback for 30 days"));
    fireEvent.click(screen.getByRole("button", { name: /start review/i }));

    await waitFor(() => expect(upload).toHaveBeenCalledWith(file, "", false, true));
  });

  it("hands the browser preview to the current result after upload", async () => {
    const scan = { id: "current-result", status: "pending" };
    vi.spyOn(apiService, "uploadScan").mockResolvedValue({ data: { scan } });
    const onUploaded = vi.fn();
    const { unmount } = render(<UploadPanel onUploaded={onUploaded} />);
    const file = new File(["RIFF"], "current.wav", { type: "audio/wav" });

    fireEvent.change(screen.getByLabelText("WAV or MP3 audio file"), { target: { files: [file] } });
    fireEvent.click(screen.getByLabelText(/consent to submit this recording/i));
    fireEvent.click(screen.getByRole("button", { name: /start review/i }));

    await waitFor(() => expect(onUploaded).toHaveBeenCalledWith(scan, { file, url: "blob:current.wav" }));
    unmount();
    expect(revokeObjectURL).not.toHaveBeenCalledWith("blob:current.wav");
  });

  it("accepts MP3 files for browser preview and upload", async () => {
    const upload = vi.spyOn(apiService, "uploadScan").mockResolvedValue({ data: { scan: { id: "mp3-scan", status: "pending" } } });
    render(<UploadPanel onUploaded={vi.fn()} />);
    const file = new File(["ID3"], "voice.mp3", { type: "audio/mpeg" });

    fireEvent.change(screen.getByLabelText("WAV or MP3 audio file"), { target: { files: [file] } });
    expect(screen.getByLabelText("Audio recording preview")).toHaveAttribute("src", "blob:voice.mp3");
    fireEvent.click(screen.getByLabelText(/consent to submit this recording/i));
    fireEvent.click(screen.getByRole("button", { name: /start review/i }));

    await waitFor(() => expect(upload).toHaveBeenCalledWith(file, "", false, false));
  });
});
