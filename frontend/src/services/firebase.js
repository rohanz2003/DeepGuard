import { getApp, getApps, initializeApp } from "firebase/app";
import { getAuth, connectAuthEmulator, inMemoryPersistence, setPersistence } from "firebase/auth";
import { firebaseAuthEmulatorUrl, firebaseConfig, isFirebaseConfigured, useFirebaseEmulators } from "../config";

let authClient;
let emulatorConnected = false;

export function getFirebaseAuth() {
  if (!isFirebaseConfigured) return null;
  const app = getApps().length ? getApp() : initializeApp(firebaseConfig);
  authClient ||= getAuth(app);
  if (!emulatorConnected && useFirebaseEmulators) {
    connectAuthEmulator(authClient, firebaseAuthEmulatorUrl, { disableWarnings: true });
    emulatorConnected = true;
  }
  return authClient;
}

export async function prepareFirebaseAuth() {
  const auth = getFirebaseAuth();
  if (auth) await setPersistence(auth, inMemoryPersistence);
  return auth;
}
