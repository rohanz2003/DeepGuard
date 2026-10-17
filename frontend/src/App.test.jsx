import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App, { AuthenticatedApp } from "./App";
import AuthPage from "./components/AuthPage";
import UploadPanel from "./components/UploadPanel";
import HistoryPage from "./pages/HistoryPage";
import HistoryTable from "./components/HistoryTable";
import ResultCard from "./components/ResultCard";
import { apiService } from "./services/api";

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

  it.each([null, undefined, "", 1.5, -0.1])("never displays invalid score %s as a real zero result", async (confidence) => {
    vi.spyOn(apiService, "getScan").mockResolvedValue({ data: { scan: {
      id: "invalid-score", filename: "fixture.wav", status: "completed",
      confidence, created_at: "2026-01-01T00:00:00Z",
    } } });
    render(<ResultCard scanId="invalid-score" onClose={vi.fn()} onDelete={vi.fn()} />);
    expect(await screen.findByText("No risk score generated")).toBeInTheDocument();
    expect(screen.queryByText(/% model score/)).not.toBeInTheDocument();
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
