import { useEffect, useState } from "react";

import Icon from "../components/Icon";
import { apiService } from "../services/api";

export default function SettingsPage({ theme, setTheme }) {
  const [model, setModel] = useState({ status: "unavailable", version: "not-configured" });

  useEffect(() => {
    let active = true;
    apiService.health().then(({ data }) => {
      if (active && data.model) setModel(data.model);
    }).catch(() => {
      if (active) setModel({ status: "unavailable", version: "not-configured" });
    });
    return () => { active = false; };
  }, []);

  const modelReady = model.status === "ready";
  return <><section className="page-heading compact"><div><p className="eyebrow">Control plane</p><h1>Workspace settings</h1><p className="page-subtitle">Keep the review experience transparent, private, and easy to audit.</p></div></section><div className="settings-grid"><article className="card settings-card"><div className="settings-icon green"><Icon name="lock" size={19} /></div><div><p className="eyebrow">Data retention</p><h2>Privacy mode is on</h2><p>Audio is processed in memory. Private scan metadata, including filenames, is retained for 30 days.</p><span className="setting-state"><span className="status-dot success" /> Active</span></div></article><article className="card settings-card"><div className="settings-icon blue"><Icon name="sparkle" size={19} /></div><div><p className="eyebrow">Inference</p><h2>{modelReady ? "Research baseline connected" : "Model not connected"}</h2><p>{modelReady ? `The API is using ${model.version}. Scores remain probabilistic indicators and require evaluation review.` : "No score or synthetic/real classification is shown until a model is supplied, evaluated, and versioned."}</p><span className="setting-state"><span className={`status-dot ${modelReady ? "success" : "neutral"}`} /> {modelReady ? model.version : "Awaiting model"}</span></div></article><article className="card settings-card"><div className="settings-icon slate"><Icon name={theme === "light" ? "sun" : "moon"} size={19} /></div><div><p className="eyebrow">Appearance</p><h2>{theme === "light" ? "Light" : "Dark"} theme</h2><p>Your theme preference is stored locally on this device and never sent to the API.</p><button className="button secondary small" onClick={() => setTheme(theme === "light" ? "dark" : "light")}>Switch to {theme === "light" ? "dark" : "light"} mode</button></div></article></div></>;
}
