import axios from "axios";
import { apiBaseUrl } from "../config";
import { getFirebaseAuth } from "./firebase";

export const api = axios.create({ baseURL: apiBaseUrl, timeout: 15000 });
export const publicApi = axios.create({ baseURL: apiBaseUrl, timeout: 10000 });

function getScanAudio(id, endpoint) {
  const path = endpoint && apiBaseUrl && endpoint.startsWith(apiBaseUrl)
    ? endpoint.slice(apiBaseUrl.length) || "/"
    : endpoint || `/scans/${encodeURIComponent(id)}/audio`;
  return api.get(path, { responseType: "blob" });
}

api.interceptors.request.use(async (config) => {
  const auth = getFirebaseAuth();
  const user = auth?.currentUser;
  if (!user) throw new Error("AUTH_REQUIRED");
  // Firebase refreshes an expired token as needed. Forcing a refresh on every
  // playback/blob request makes a transient Firebase network hiccup look like
  // the API itself is unavailable.
  const token = await user.getIdToken();
  if (!auth.currentUser || auth.currentUser.uid !== user.uid) throw new Error("AUTH_CHANGED");
  config.headers = config.headers || {};
  config.headers.Authorization = `Bearer ${token}`;
  return config;
});

export const apiService = {
  health: () => publicApi.get("/health"),
  verifySession: () => api.get("/auth/me"),
  listScans: () => api.get("/scans"),
  getScan: (id) => api.get(`/scans/${encodeURIComponent(id)}`),
  uploadScan: (file, transcript = "", transcriptConsent = false, retainAudio = false) => {
    // Keep the service tolerant of an options object while the public call
    // remains backward-compatible with uploadScan(file).
    if (transcript && typeof transcript === "object") {
      const options = transcript;
      transcriptConsent = options.transcriptConsent ?? options.transcript_consent ?? false;
      retainAudio = options.retainAudio ?? options.retain_audio ?? false;
      transcript = options.transcript || "";
    } else if (transcriptConsent && typeof transcriptConsent === "object") {
      const options = transcriptConsent;
      transcriptConsent = options.transcriptConsent ?? options.transcript_consent ?? false;
      retainAudio = options.retainAudio ?? options.retain_audio ?? retainAudio;
    }
    const body = new FormData();
    body.append("audio", file);
    body.append("consent", "true");
    body.append("transcript", transcript || "");
    body.append("transcript_consent", transcriptConsent ? "true" : "false");
    body.append("retain_audio", retainAudio ? "true" : "false");
    return api.post("/scans", body);
  },
  getScanAudio,
  getScanPlayback: (...args) => apiService.getScanAudio(...args),
  deleteScan: (id) => api.delete(`/scans/${encodeURIComponent(id)}`),
};

export function apiErrorMessage(error, fallback) {
  if (error?.code === "auth/network-request-failed") return "Your sign-in session could not be refreshed. Check your connection and sign in again.";
  if (error?.code === "ERR_NETWORK" || (error?.request && !error?.response)) return "The API server could not be reached. Check that the backend is running.";
  if (error?.code === "ECONNABORTED" || error?.message === "Network Error") return "The API server could not be reached. Check that the backend is running.";
  return error?.response?.data?.error || fallback;
}
