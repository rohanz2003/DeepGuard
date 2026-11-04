import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { sendPasswordResetEmail } from "firebase/auth";
import { getFirebaseAuth } from "../services/firebase";
import Icon from "./Icon";

export default function AuthPage({ authenticate, initialError = "" }) {
  const [mode, setMode] = useState("login");
  const [form, setForm] = useState({ email: "", password: "" });
  const [state, setState] = useState({ loading: false, error: initialError });
  const [showPassword, setShowPassword] = useState(false);
  const [forgotEmail, setForgotEmail] = useState("");
  const [forgotState, setForgotState] = useState({ loading: false, error: "", success: "" });
  const navigate = useNavigate();

  const submit = async (event) => {
    event.preventDefault();
    setState({ loading: true, error: "" });
    try { await authenticate(mode, form.email.trim(), form.password); navigate("/", { replace: true }); }
    catch (error) { setState({ loading: false, error: firebaseError(error) }); }
  };

  const handleForgotPassword = async (event) => {
    event.preventDefault();
    const email = forgotEmail.trim();
    if (!email) { setForgotState({ loading: false, error: "Enter your email address.", success: "" }); return; }
    setForgotState({ loading: true, error: "", success: "" });
    try {
      const auth = getFirebaseAuth();
      if (!auth) throw new Error("Firebase is not configured.");
      await sendPasswordResetEmail(auth, email);
      setForgotState({ loading: false, error: "", success: "Password reset email sent. Check your inbox." });
      setForgotEmail("");
    } catch (error) {
      setForgotState({ loading: false, error: firebaseError(error), success: "" });
    }
  };

  const toggleShowPassword = () => setShowPassword(!showPassword);

  return <main className="auth-shell"><section className="auth-visual"><div className="auth-orbit orbit-one" /><div className="auth-orbit orbit-two" /><div className="brand-mark large"><Icon name="shield" size={22} /></div><p className="eyebrow light">Private by design</p><h1>Review the voice.<br /><em>Trust the evidence.</em></h1><p className="auth-lede">A focused workspace for inspecting suspicious recordings without turning uncertain model output into certainty.</p><div className="auth-proof"><span><Icon name="lock" size={15} /> Raw audio not retained</span><span><Icon name="check" size={15} /> Auditable scan history</span></div></section><section className="auth-panel"><div className="auth-card"><div className="mobile-brand"><div className="brand-mark"><Icon name="shield" size={20} /></div><strong>DeepGuard</strong></div><p className="eyebrow">Secure workspace</p><h2>{mode === "login" ? "Welcome back" : mode === "forgot" ? "Reset password" : "Create your workspace"}</h2><p className="auth-copy">{mode === "login" ? "Sign in to continue reviewing your scan history." : mode === "forgot" ? "Enter your email and we'll send a password reset link." : "Create a Firebase account for your private review workspace."}</p>
    {mode === "forgot" ? (
      <form onSubmit={handleForgotPassword} className="auth-form">
        <label>Email address<input type="email" value={forgotEmail} onChange={(event) => setForgotEmail(event.target.value)} autoComplete="email" required /></label>
        {forgotState.error && <div className="form-error" role="alert"><Icon name="alert" size={16} /> {forgotState.error}</div>}
        {forgotState.success && <div className="form-error form-success" role="status"><Icon name="check" size={16} /> {forgotState.success}</div>}
        <button className="button primary full" disabled={forgotState.loading}>{forgotState.loading ? <><span className="button-spinner" /> Sending…</> : "Send reset link"}</button>
      </form>
    ) : (
      <form onSubmit={submit} className="auth-form">
        <label>Email address<input type="email" value={form.email} onChange={(event) => setForm({ ...form, email: event.target.value })} autoComplete="email" required /></label>
        <label className="password-field">
          Password
          <div className="password-input-wrapper">
            <input type={showPassword ? "text" : "password"} value={form.password} onChange={(event) => setForm({ ...form, password: event.target.value })} minLength={8} autoComplete={mode === "login" ? "current-password" : "new-password"} required />
            <button type="button" className="password-toggle" onClick={toggleShowPassword} aria-label={showPassword ? "Hide password" : "Show password"}><Icon name={showPassword ? "eye-off" : "eye"} size={18} /></button>
          </div>
        </label>
        {mode === "login" && <button type="button" className="forgot-inline" onClick={() => { setMode("forgot"); setState({ loading: false, error: "" }); }}>Forgot password?</button>}
        {state.error && <div className="form-error" role="alert"><Icon name="alert" size={16} /> {state.error}</div>}
        <button className="button primary full" disabled={state.loading}>{state.loading ? <><span className="button-spinner" /> Connecting…</> : <>{mode === "login" ? "Sign in" : "Create account"}<Icon name="arrow" size={17} /></>}</button>
      </form>
    )}
    <div className="auth-switch">
      {mode === "login" && <><span>New to DeepGuard?</span><button type="button" className="link-button" onClick={() => { setMode("register"); setState({ loading: false, error: "" }); }}>Create an account</button></>}
      {mode === "register" && <><span>Already have an account?</span><button type="button" className="link-button" onClick={() => { setMode("login"); setState({ loading: false, error: "" }); }}>Sign in instead</button></>}
      {mode === "forgot" && <><span>Remember your password?</span><button type="button" className="link-button" onClick={() => { setMode("login"); setForgotState({ loading: false, error: "", success: "" }); }}>Back to sign in</button></>}
    </div>
    <p className="auth-disclaimer"><Icon name="info" size={14} /> Firebase Authentication protects your session in memory.</p></div></section></main>;
}

function firebaseError(error) {
  const messages = { "auth/invalid-credential": "The email or password is incorrect.", "auth/email-already-in-use": "An account already exists for this email.", "auth/weak-password": "Choose a stronger password (at least 8 characters).", "auth/invalid-email": "Enter a valid email address.", "auth/user-not-found": "No account found for this email.", "auth/too-many-requests": "Too many attempts. Please try again later." };
  return messages[error?.code] || "Authentication could not be completed. Check your connection and try again.";
}
