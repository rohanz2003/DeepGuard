import { useState } from "react";
import { useNavigate } from "react-router-dom";
import Icon from "./Icon";

export default function AuthPage({ authenticate, initialError = "" }) {
  const [mode, setMode] = useState("login");
  const [form, setForm] = useState({ email: "", password: "" });
  const [state, setState] = useState({ loading: false, error: initialError });
  const navigate = useNavigate();
  const submit = async (event) => {
    event.preventDefault();
    setState({ loading: true, error: "" });
    try { await authenticate(mode, form.email.trim(), form.password); navigate("/", { replace: true }); }
    catch (error) { setState({ loading: false, error: firebaseError(error) }); }
  };
  return <main className="auth-shell"><section className="auth-visual"><div className="auth-orbit orbit-one" /><div className="auth-orbit orbit-two" /><div className="brand-mark large"><Icon name="shield" size={22} /></div><p className="eyebrow light">Private by design</p><h1>Review the voice.<br /><em>Trust the evidence.</em></h1><p className="auth-lede">A focused workspace for inspecting suspicious recordings without turning uncertain model output into certainty.</p><div className="auth-proof"><span><Icon name="lock" size={15} /> Raw audio not retained</span><span><Icon name="check" size={15} /> Auditable scan history</span></div></section><section className="auth-panel"><div className="auth-card"><div className="mobile-brand"><div className="brand-mark"><Icon name="shield" size={20} /></div><strong>DeepGuard</strong></div><p className="eyebrow">Secure workspace</p><h2>{mode === "login" ? "Welcome back" : "Create your workspace"}</h2><p className="auth-copy">{mode === "login" ? "Sign in to continue reviewing your scan history." : "Create a Firebase account for your private review workspace."}</p><form onSubmit={submit} className="auth-form"><label>Email address<input type="email" value={form.email} onChange={(event) => setForm({ ...form, email: event.target.value })} autoComplete="email" required /></label><label>Password<input type="password" value={form.password} onChange={(event) => setForm({ ...form, password: event.target.value })} minLength={8} autoComplete={mode === "login" ? "current-password" : "new-password"} required /></label>{state.error && <div className="form-error" role="alert"><Icon name="alert" size={16} /> {state.error}</div>}<button className="button primary full" disabled={state.loading}>{state.loading ? <><span className="button-spinner" /> Connecting…</> : <>{mode === "login" ? "Sign in" : "Create account"}<Icon name="arrow" size={17} /></>}</button></form><div className="auth-switch">{mode === "login" ? "New to DeepGuard?" : "Already have an account?"}<button type="button" className="link-button" onClick={() => { setMode(mode === "login" ? "register" : "login"); setState({ loading: false, error: "" }); }}>{mode === "login" ? "Create an account" : "Sign in instead"}</button></div><p className="auth-disclaimer"><Icon name="info" size={14} /> Firebase Authentication protects your session in memory.</p></div></section></main>;
}

function firebaseError(error) {
  const messages = { "auth/invalid-credential": "The email or password is incorrect.", "auth/email-already-in-use": "An account already exists for this email.", "auth/weak-password": "Choose a stronger password (at least 8 characters).", "auth/invalid-email": "Enter a valid email address." };
  return messages[error?.code] || "Authentication could not be completed. Check your connection and try again.";
}
