import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App, { AuthenticatedApp } from "./App";
import AuthPage from "./components/AuthPage";
import UploadPanel from "./components/UploadPanel";
import WorkspaceLayout from "./components/WorkspaceLayout";
import HistoryPage from "./pages/HistoryPage";
import HistoryTable from "./components/HistoryTable";
import ResultCard from "./components/ResultCard";
import { api, apiService } from "./services/api";

vi.mock("./hooks/useAuth", () => ({ useAuth: () => ({ configured: false, user: null, verifiedUser: null, checking: false, authenticate: vi.fn(), logout: vi.fn() }) }));
vi.mock("./services/firebase", () => ({ getFirebaseAuth: () => null, prepareFirebaseAuth: vi.fn() }));
vi.mock("./config", async (importOriginal) => ({ ...(await importOriginal()), isFirebaseConfigured: false, clearLegacyCredentials: vi.fn() }));

describe("frontend safety paths", () => {
  beforeEach(() => { localStorage.clear(); vi.restoreAllMocks(); vi.clearAllMocks(); });

  it("renders setup instructions without crashing when Firebase is missing", () => {
    render(<MemoryRouter initialEntries={["/login"]}><App /></MemoryRouter>);
    expect(screen.getByText("Finish local configuration")).toBeInTheDocument();
    expect(screen.getByText(/VITE_FIREBASE_API_KEY/)).toBeInTheDocument();
  });

  it("supports both Firebase login and registration actions", async () => {
    const authenticate = vi.fn().mockResolvedValue({});
    render(<MemoryRouter><AuthPage authenticate={authenticate} /></MemoryRouter>);
    fireEvent.change(screen.getByLabelText(/email address/i), { target: { value: "person@example.com" } });
    fireEvent.change(screen.getByLabelText(/password/i), { target: { value: "strong-password" } });
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));
    await waitFor(() => expect(authenticate).toHaveBeenCalledWith("login", "person@example.com", "strong-password"));
    fireEvent.click(screen.getByRole("button", { name: /create an account/i }));
    fireEvent.click(screen.getByRole("button", { name: /create account/i }));
    await waitFor(() => expect(authenticate).toHaveBeenCalledWith("register", "person@example.com", "strong-password"));
  });

  it("shows protected server verification failures separately from login", () => {
    render(<AuthenticatedApp auth={{ checking: false, user: { uid: "u" }, verifiedUser: null, errorKind: "server", error: "API is offline", authenticate: vi.fn(), logout: vi.fn() }} />);
    expect(screen.getByRole("alert")).toHaveTextContent("API is offline");
    expect(screen.getByRole("button", { name: /retry verification/i })).toBeInTheDocument();
  });

  it("provides an accessible delete control for each stored record", () => {
    const onDelete = vi.fn();
    render(<HistoryTable scans={[{ id: "scan-1", filename: "voice.wav", size_bytes: 100, duration_seconds: 1, status: "unavailable", model_version: "not-configured", created_at: "2026-01-01T00:00:00Z" }]} loading={false} onSelect={vi.fn()} onDelete={onDelete} />);
    fireEvent.click(screen.getByRole("button", { name: /delete voice\.wav/i }));
    expect(onDelete).toHaveBeenCalledWith("scan-1");
  });

  it("requires consent before upload and marks feedback as an alert", async () => {
    render(<UploadPanel onUploaded={vi.fn()} />);
    const file = new File(["RIFF"], "voice.wav", { type: "audio/wav" });
    const input = screen.getByLabelText("WAV audio file");
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() => expect(screen.getByText(/Ready to validate/)).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /start review/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/consent/i);
  });

  it("requires separate transcript consent when optional transcript text is present", async () => {
    const upload = vi.spyOn(apiService, "uploadScan").mockResolvedValue({ data: { scan: { id: "transcript-scan", status: "unavailable" } } });
    render(<UploadPanel onUploaded={vi.fn()} />);
    expect(screen.queryByLabelText(/consent to analyze this transcript/i)).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("WAV audio file"), {
      target: { files: [new File(["RIFF"], "fixture.wav", { type: "audio/wav" })] },
    });
    fireEvent.change(screen.getByLabelText(/transcript.*optional/i), { target: { value: "Please verify this payment immediately." } });
    expect(screen.getByLabelText(/consent to analyze this transcript/i)).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText(/consent to submit this recording/i));
    fireEvent.click(screen.getByRole("button", { name: /start review/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/transcript/i);
    expect(upload).not.toHaveBeenCalled();
  });

  it("shows the transcript counter and accessible over-limit validation", () => {
    render(<UploadPanel onUploaded={vi.fn()} />);
    const transcript = screen.getByLabelText(/transcript.*optional/i);
    expect(screen.getByText("0 / 12000 characters")).toBeInTheDocument();
    fireEvent.change(transcript, { target: { value: "hello" } });
    expect(screen.getByText("5 / 12000 characters")).toBeInTheDocument();
    fireEvent.change(transcript, { target: { value: "x".repeat(12001) } });
    expect(screen.getByText("12001 / 12000 characters")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/12000 characters or fewer/i);
  });

  it("sends audio, transcript, and both consent fields in the multipart payload", async () => {
    const post = vi.spyOn(api, "post").mockResolvedValue({ data: { scan: {} } });
    const file = new File(["RIFF"], "fixture.wav", { type: "audio/wav" });
    await apiService.uploadScan(file, "Urgent payment request", true);
    const body = post.mock.calls[0][1];
    expect(body.get("audio")).toBe(file);
    expect(body.get("consent")).toBe("true");
    expect(body.get("transcript")).toBe("Urgent payment request");
    expect(body.get("transcript_consent")).toBe("true");
  });

  it("shows completed research analysis after a consented WAV upload", async () => {
    const scan = { id: "research-scan", status: "completed", confidence: 0.63 };
    vi.spyOn(apiService, "uploadScan").mockResolvedValue({ data: { scan } });
    const onUploaded = vi.fn();
    render(<UploadPanel onUploaded={onUploaded} />);
    fireEvent.change(screen.getByLabelText("WAV audio file"), {
      target: { files: [new File(["RIFF"], "fixture.wav", { type: "audio/wav" })] },
    });
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: /start review/i }));
    await waitFor(() => expect(onUploaded).toHaveBeenCalledWith(scan));
    expect(screen.getByText(/Research analysis completed/)).toBeInTheDocument();
    expect(screen.queryByText(/Inference remains unavailable/)).not.toBeInTheDocument();
  });

  it("keeps fresh upload scam-language fields when an older history response resolves later", async () => {
    const pendingLists = [];
    const listScans = vi.spyOn(apiService, "listScans").mockImplementation(() => new Promise((resolve) => pendingLists.push(resolve)));
    const uploadScan = vi.spyOn(apiService, "uploadScan").mockResolvedValue({ data: { scan: {
      id: "fresh-upload", filename: "fresh.wav", status: "completed", deepfake_status: "high", confidence: 0.84,
      scam_status: "review", scam_score: 0.91, scam_categories: ["Urgency"], scam_explanation: "Fresh language result.",
      transcript: "This private transcript must never be rendered.", created_at: "2026-01-01T00:00:00Z",
    } } });
    vi.spyOn(apiService, "health").mockResolvedValue({ data: { model: { status: "unavailable" } } });

    render(<MemoryRouter><WorkspaceLayout user={{ id: "user-1", email: "person@example.com" }} onSignOut={vi.fn()} /></MemoryRouter>);
    await waitFor(() => expect(listScans).toHaveBeenCalledTimes(1));
    fireEvent.change(screen.getByLabelText("WAV audio file"), { target: { files: [new File(["RIFF"], "fresh.wav", { type: "audio/wav" })] } });
    fireEvent.click(screen.getByLabelText(/consent to submit this recording/i));
    fireEvent.click(screen.getByRole("button", { name: /start review/i }));
    await waitFor(() => expect(uploadScan).toHaveBeenCalled());
    await waitFor(() => expect(listScans).toHaveBeenCalledTimes(2));
    expect(screen.getByText("Fresh language result.")).toBeInTheDocument();
    expect(screen.getByText("Urgency")).toBeInTheDocument();
    expect(screen.getByText("91%")).toBeInTheDocument();
    expect(screen.queryByText("This private transcript must never be rendered.")).not.toBeInTheDocument();

    const staleResponse = { data: { scans: [{
      id: "fresh-upload", filename: "fresh.wav", status: "completed", deepfake_status: "inconclusive", confidence: null,
      scam_status: "unavailable", scam_score: null, scam_categories: [], scam_explanation: "No transcript was provided.",
      created_at: "2026-01-01T00:00:00Z",
    }], stats: { total_scans: 1, suspicious_results: 0, analyzed_results: 0, inconclusive_results: 1 } } };
    pendingLists[0](staleResponse);
    pendingLists[1](staleResponse);
    await waitFor(() => {
      expect(screen.getByText("Fresh language result.")).toBeInTheDocument();
      expect(screen.getByText("Urgency")).toBeInTheDocument();
      expect(screen.getByText("91%")).toBeInTheDocument();
    });
    expect(screen.queryByText("No transcript was provided.")).not.toBeInTheDocument();
  });

  it.each([null, undefined, "", 1.5, -0.1])("never displays invalid score %s as a real zero result", async (confidence) => {
    vi.spyOn(apiService, "getScan").mockResolvedValue({ data: { scan: {
      id: "invalid-score", filename: "fixture.wav", status: "completed",
      confidence, created_at: "2026-01-01T00:00:00Z",
    } } });
    render(<ResultCard scanId="invalid-score" onClose={vi.fn()} onDelete={vi.fn()} />);
    expect(await screen.findByText("No risk score generated")).toBeInTheDocument();
    expect(screen.queryByText(/% model score/)).not.toBeInTheDocument();
  });

  it("shows scam-language analysis as unavailable when no transcript was supplied", () => {
    render(<ResultCard initialScan={{ id: "no-transcript", filename: "fixture.wav", status: "completed", deepfake_status: "low", confidence: 0.22, scam_status: "unavailable" }} onClose={vi.fn()} onDelete={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "Scam-language signal" })).toBeInTheDocument();
    expect(screen.getByText("No transcript supplied, so scam-language analysis is unavailable.")).toBeInTheDocument();
    expect(screen.getAllByText("Unavailable").length).toBeGreaterThan(0);
  });

  it("keeps voice authenticity and scam-language signals separate", () => {
    const rawTranscript = "Your account is locked; send a payment code now.";
    render(<ResultCard initialScan={{ id: "separate-signals", filename: "fixture.wav", status: "completed", deepfake_status: "high", confidence: 0.81, explanation: "Voice signal caveat.", scam_status: "review", scam_score: 0.72, scam_categories: ["Urgency", "Payment request"], scam_explanation: "Language signal caveat.", transcript: rawTranscript }} onClose={vi.fn()} onDelete={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "Voice authenticity signal" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Scam-language signal" })).toBeInTheDocument();
    expect(screen.getByText("81%")).toBeInTheDocument();
    expect(screen.getByText("72%")).toBeInTheDocument();
    expect(screen.getByText("Higher signal")).toBeInTheDocument();
    expect(screen.getByText("Needs review")).toBeInTheDocument();
    expect(screen.getByText("Urgency")).toBeInTheDocument();
    expect(screen.getByText("Language signal caveat.")).toBeInTheDocument();
    expect(screen.queryByText(rawTranscript)).not.toBeInTheDocument();
  });

  it("keeps a consented scam result when a detail read is stale", () => {
    const getScan = vi.spyOn(apiService, "getScan").mockResolvedValue({ data: { scan: { id: "stale-detail", scam_status: "unavailable", scam_score: null, scam_categories: [], scam_explanation: "No transcript was provided." } } });
    render(<ResultCard scanId="stale-detail" initialScan={{ id: "stale-detail", filename: "fixture.wav", status: "completed", deepfake_status: "inconclusive", scam_status: "review", scam_score: 0.4, scam_categories: ["financial_request"], scam_explanation: "Consent-gated local analysis matched a financial request." }} onClose={vi.fn()} onDelete={vi.fn()} />);
    expect(screen.getByText("40%")).toBeInTheDocument();
    expect(screen.getByText("financial_request")).toBeInTheDocument();
    expect(screen.queryByText("No transcript supplied")).not.toBeInTheDocument();
    expect(getScan).not.toHaveBeenCalled();
  });

  it("keeps preview history empty and routes its CTA to sign in", () => {
    render(<MemoryRouter><HistoryPage data={{ scans: [], stats: { total_scans: 0 } }} loading={false} navigate={vi.fn()} readOnly /></MemoryRouter>);
    expect(screen.getByText("Preview history is empty")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /sign in/i })).toBeInTheDocument();
  });

  it("only persists non-sensitive preferences", async () => {
    render(<MemoryRouter initialEntries={["/preview"]}><App /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText(/Read-only Preview workspace/)).toBeInTheDocument());
    expect(localStorage.getItem("deepguard-token")).toBeNull();
    expect(Object.keys(localStorage)).not.toContain("firebase:authUser");
  });
});
