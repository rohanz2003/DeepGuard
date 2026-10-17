import { useEffect, useRef, useState } from "react";
import { createUserWithEmailAndPassword, onAuthStateChanged, signInWithEmailAndPassword, signOut } from "firebase/auth";
import { isFirebaseConfigured } from "../config";
import { apiErrorMessage, apiService } from "../services/api";
import { getFirebaseAuth, prepareFirebaseAuth } from "../services/firebase";

export function useAuth() {
  const [state, setState] = useState({ configured: isFirebaseConfigured, user: null, verifiedUser: null, checking: isFirebaseConfigured, error: "", errorKind: "" });
  const generation = useRef(0);

  useEffect(() => {
    if (!isFirebaseConfigured) return undefined;
    let active = true;
    let unsubscribe;
    prepareFirebaseAuth().then((auth) => {
      if (!auth || !active) return;
      unsubscribe = onAuthStateChanged(auth, async (user) => {
        const current = ++generation.current;
        if (!user) {
          setState((old) => ({ ...old, user: null, verifiedUser: null, checking: false, error: "", errorKind: "" }));
          return;
        }
        setState((old) => ({ ...old, user, checking: true, error: "", errorKind: "" }));
        try {
          const { data } = await apiService.verifySession();
          if (!active || current !== generation.current) return;
          setState((old) => ({ ...old, user, verifiedUser: data.user, checking: false }));
        } catch (error) {
          if (!active || current !== generation.current) return;
          const unauthorized = error?.response?.status === 401;
          setState((old) => ({ ...old, user: unauthorized ? null : user, verifiedUser: null, checking: false, error: apiErrorMessage(error, "The server could not verify this Firebase session."), errorKind: unauthorized ? "auth" : "server" }));
          if (unauthorized) {
            await signOut(auth);
            if (active) setState((old) => ({ ...old, user: null, verifiedUser: null, checking: false, error: "Your Firebase session was rejected by the API. Please sign in again.", errorKind: "auth" }));
          }
        }
      });
    }).catch((error) => active && setState((old) => ({ ...old, checking: false, error: error.message || "Firebase could not be initialized.", errorKind: "config" })));
    return () => { active = false; generation.current += 1; unsubscribe?.(); };
  }, []);

  const authenticate = async (mode, email, password) => {
    const auth = getFirebaseAuth();
    if (!auth) throw new Error("Firebase is not configured.");
    return mode === "register" ? createUserWithEmailAndPassword(auth, email, password) : signInWithEmailAndPassword(auth, email, password);
  };
  const logout = async () => { const auth = getFirebaseAuth(); generation.current += 1; if (auth) await signOut(auth); };
  return { ...state, authenticate, logout };
}
