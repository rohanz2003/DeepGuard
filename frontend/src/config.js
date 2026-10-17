export const firebaseConfig = {
  apiKey: import.meta.env.VITE_FIREBASE_API_KEY,
  authDomain: import.meta.env.VITE_FIREBASE_AUTH_DOMAIN,
  projectId: import.meta.env.VITE_FIREBASE_PROJECT_ID,
  appId: import.meta.env.VITE_FIREBASE_APP_ID,
};

export const isFirebaseConfigured = Object.values(firebaseConfig).every(Boolean);
export const useFirebaseEmulators = import.meta.env.VITE_USE_FIREBASE_EMULATORS === "true";
export const firebaseAuthEmulatorUrl = import.meta.env.VITE_FIREBASE_AUTH_EMULATOR_URL || "http://127.0.0.1:9099";
export const apiBaseUrl = import.meta.env.VITE_API_URL || "/api";

export const PREFERENCE_KEYS = ["deepguard-theme", "deepguard-sidebar", "deepguard-preferred-tab"];

export function clearLegacyCredentials() {
  // Older local-JWT builds wrote this key. It must never coexist with Firebase auth.
  window.localStorage.removeItem("deepguard-token");
}
