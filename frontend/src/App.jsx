import { useEffect } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import AuthPage from "./components/AuthPage";
import ErrorBoundary from "./components/ErrorBoundary";
import SetupPage from "./components/SetupPage";
import WorkspaceLayout from "./components/WorkspaceLayout";
import NotFoundPage from "./pages/NotFoundPage";
import { clearLegacyCredentials, isFirebaseConfigured } from "./config";
import { useAuth } from "./hooks/useAuth";

export default function App() {
  const auth = useAuth();
  useEffect(() => { clearLegacyCredentials(); }, []);
  useEffect(() => { document.documentElement.dataset.theme = localStorage.getItem("deepguard-theme") || "light"; }, []);
  return <ErrorBoundary><Routes><Route path="/preview/*" element={<WorkspaceLayout readOnly />} />{!isFirebaseConfigured ? <Route path="*" element={<SetupPage />} /> : <Route path="*" element={<AuthenticatedApp auth={auth} />} />}</Routes></ErrorBoundary>;
}

export function AuthenticatedApp({ auth }) {
  if (auth.checking) return <div className="app-loading" role="status"><div className="loader" /><span>{auth.user ? "Verifying secure workspace" : "Loading secure workspace"}</span></div>;
  if (auth.errorKind === "server") return <main className="app-loading" role="alert"><h1>Workspace verification unavailable</h1><p>{auth.error}</p><button className="button primary" onClick={() => window.location.reload()}>Retry verification</button></main>;
  return <Routes><Route path="/login" element={auth.user ? <Navigate to="/" replace /> : <AuthPage authenticate={auth.authenticate} initialError={auth.errorKind === "auth" ? auth.error : ""} />} /><Route path="/*" element={auth.verifiedUser ? <WorkspaceLayout user={auth.verifiedUser} onSignOut={auth.logout} /> : <Navigate to="/login" replace />} /><Route path="*" element={<NotFoundPage />} /></Routes>;
}
