import { Link } from "react-router-dom";
import Icon from "../components/Icon";

export default function LandingPage() {
  return (
    <div className="landing-shell">
      <header className="landing-header">
        <div className="landing-container">
          <Link to="/login" className="landing-brand">
            <span className="landing-brand-mark"><Icon name="shield" size={24} /></span>
            <span className="landing-brand-name">Deep<span>Guard</span></span>
          </Link>
          <nav className="landing-nav">
            <Link to="/login" className="landing-nav-link">Sign in</Link>
            <Link to="/login" className="button primary landing-cta">Get started</Link>
          </nav>
        </div>
      </header>

      <main className="landing-main">
        <section className="hero">
          <div className="landing-container">
            <div className="hero-content">
              <p className="hero-eyebrow">Voice risk review workspace</p>
              <h1 className="hero-title">Review the voice.<br /><em>Trust the evidence.</em></h1>
              <p className="hero-description">
                A focused, private workspace for inspecting suspicious recordings without turning uncertain model output into certainty.
                Upload consented audio, review separate voice-authenticity and scam-language signals, and maintain an auditable scan history.
              </p>
              <div className="hero-actions">
                <Link to="/login" className="button primary hero-cta">
                  Start reviewing <Icon name="arrow" size={17} />
                </Link>
                <Link to="/login" className="button secondary hero-secondary">See how it works</Link>
              </div>
              <div className="hero-trust">
                <span className="hero-trust-item"><Icon name="lock" size={14} /> Raw audio never retained</span>
                <span className="hero-trust-item"><Icon name="check" size={14} /> Auditable scan history</span>
                <span className="hero-trust-item"><Icon name="shield" size={14} /> Private by design</span>
              </div>
            </div>
            <div className="hero-visual">
              <div className="hero-graphic">
                <div className="hero-orb orb-1" />
                <div className="hero-orb orb-2" />
                <div className="hero-orb orb-3" />
                <div className="hero-waveform" aria-hidden="true">
                  <svg viewBox="0 0 200 80" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                    <path d="M10 40 Q25 20 40 40 T70 40 T100 40 T130 40 T160 40 T190 40" />
                  </svg>
                </div>
              </div>
            </div>
          </div>
        </section>

        <section className="features">
          <div className="landing-container">
            <div className="features-header">
              <p className="eyebrow">Capabilities</p>
              <h2>Built for careful review</h2>
            </div>
            <div className="features-grid">
              <article className="feature-card">
                <div className="feature-icon blue"><Icon name="waveform" size={22} /></div>
                <h3>Voice authenticity</h3>
                <p>Separate deepfake detection signal analyzes spectral and temporal artifacts to flag synthetic or manipulated audio.</p>
              </article>
              <article className="feature-card">
                <div className="feature-icon amber"><Icon name="alert" size={22} /></div>
                <h3>Scam language</h3>
                <p>Independent NLP signal detects social-engineering patterns — urgency, authority impersonation, credential requests.</p>
              </article>
              <article className="feature-card">
                <div className="feature-icon green"><Icon name="history" size={22} /></div>
                <h3>Scan history</h3>
                <p>Private, auditable log of every upload with status, model version, and timestamp. Export or delete anytime.</p>
              </article>
              <article className="feature-card">
                <div className="feature-icon slate"><Icon name="lock" size={22} /></div>
                <h3>Privacy first</h3>
                <p>Raw audio is processed in-memory and never persisted. Only derived signals and metadata are stored.</p>
              </article>
              <article className="feature-card">
                <div className="feature-icon blue"><Icon name="radio" size={22} /></div>
                <h3>Live calls (planned)</h3>
                <p>Real-time risk scoring during active calls with on-device processing for immediate feedback.</p>
              </article>
              <article className="feature-card">
                <div className="feature-icon amber"><Icon name="settings" size={22} /></div>
                <h3>Workspace settings</h3>
                <p>Configure model preferences, retention policies, and notification thresholds per workspace.</p>
              </article>
            </div>
          </div>
        </section>

        <section className="workflow">
          <div className="landing-container">
            <div className="workflow-header">
              <p className="eyebrow">How it works</p>
              <h2>Three steps to evidence</h2>
            </div>
            <div className="workflow-steps">
              <div className="workflow-step">
                <div className="workflow-number">1</div>
                <div className="workflow-content">
                  <h3>Upload recording</h3>
                  <p>Drag and drop a consented WAV file. Audio is validated for quality and duration before analysis.</p>
                </div>
              </div>
              <div className="workflow-step">
                <div className="workflow-number">2</div>
                <div className="workflow-content">
                  <h3>Review signals</h3>
                  <p>Inspect separate voice-authenticity and scam-language scores with segment-level evidence and explanations.</p>
                </div>
              </div>
              <div className="workflow-step">
                <div className="workflow-number">3</div>
                <div className="workflow-content">
                  <h3>Act on evidence</h3>
                  <p>Save to history, export reports, or delete. Every action is logged in your private audit trail.</p>
                </div>
              </div>
            </div>
          </div>
        </section>

        <section className="cta-section">
          <div className="landing-container">
            <div className="cta-card">
              <h2>Start your private workspace</h2>
              <p>Create a Firebase-backed account in seconds. No credit card, no raw audio retention.</p>
              <Link to="/login" className="button primary cta-button">
                Create free account <Icon name="arrow" size={17} />
              </Link>
            </div>
          </div>
        </section>
      </main>

      <footer className="landing-footer">
        <div className="landing-container">
          <div className="footer-grid">
            <div className="footer-brand">
              <Link to="/login" className="footer-brand-link">
                <span className="footer-brand-mark"><Icon name="shield" size={20} /></span>
                <span className="footer-brand-name">Deep<span>Guard</span></span>
              </Link>
              <p className="footer-tagline">Voice risk review workspace — private by design.</p>
            </div>
            <nav className="footer-nav">
              <h4>Product</h4>
              <ul>
                <li><Link to="/login">Overview</Link></li>
                <li><Link to="/login">Scan history</Link></li>
                <li><Link to="/login">Settings</Link></li>
              </ul>
            </nav>
            <nav className="footer-nav">
              <h4>Resources</h4>
              <ul>
                <li><a href="#" target="_blank" rel="noopener noreferrer">Documentation</a></li>
                <li><a href="#" target="_blank" rel="noopener noreferrer">Privacy policy</a></li>
                <li><a href="#" target="_blank" rel="noopener noreferrer">Terms of service</a></li>
              </ul>
            </nav>
            <nav className="footer-nav">
              <h4>Legal</h4>
              <ul>
                <li><a href="#" target="_blank" rel="noopener noreferrer">Cookie policy</a></li>
                <li><a href="#" target="_blank" rel="noopener noreferrer">Security</a></li>
              </ul>
            </nav>
          </div>
          <div className="footer-bottom">
            <p className="footer-copyright">© {new Date().getFullYear()} DeepGuard. All rights reserved.</p>
            <div className="footer-social">
              <a href="#" target="_blank" rel="noopener noreferrer" aria-label="GitHub"><Icon name="sparkle" size={18} /></a>
              <a href="#" target="_blank" rel="noopener noreferrer" aria-label="Twitter"><Icon name="radio" size={18} /></a>
            </div>
          </div>
        </div>
      </footer>
    </div>
  );
}