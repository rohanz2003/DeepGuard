import axios from "axios";
import { apiBaseUrl } from "../config";
import { getFirebaseAuth } from "./firebase";

export const api = axios.create({ baseURL: apiBaseUrl, timeout: 15000 });
export const publicApi = axios.create({ baseURL: apiBaseUrl, timeout: 10000 });

api.interceptors.request.use(async (config) => {
  const auth = getFirebaseAuth();
  const user = auth?.currentUser;
  if (!user) throw new Error("AUTH_REQUIRED");
  const token = await user.getIdToken(true);
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
  uploadScan: (file) => {
    const body = new FormData();
    body.append("audio", file);
    body.append("consent", "true");
    return api.post("/scans", body);
  },
  deleteScan: (id) => api.delete(`/scans/${encodeURIComponent(id)}`),
};

export function apiErrorMessage(error, fallback) {
  if (error?.code === "ECONNABORTED" || error?.message === "Network Error") return "The API server could not be reached. Check that the backend is running.";
  return error?.response?.data?.error || fallback;
}
