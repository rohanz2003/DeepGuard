import { useEffect, useState } from "react";
import Icon from "./Icon";
import StatusBadge from "./StatusBadge";
import { apiErrorMessage, apiService } from "../services/api";
import { formatBytes, formatDate, formatDuration } from "../utils/format";
import { normalizeScan } from "../utils/scan";

export default function ResultCard({ scanId, initialScan, onDelete, onClose }) {
  const [state, setState] = useState({ scan: initialScan || null, loading: Boolean(scanId), error: "" });
  useEffect(() => {
    let active = true;
    setState({ scan: initialScan || null, loading: Boolean(scanId), error: "" });
    if (scanId) apiService.getScan(scanId).then(({ data }) => active && setState({ scan: normalizeScan(data.scan || data), loading: false, error: "" })).catch((error) => active && setState({ scan: null, loading: false, error: apiErrorMessage(error, "Could not load this scan detail.") }));
    return () => { active = false; };
  }, [scanId, initialScan]);
  if (state.loading) return <div className="result-card" role="status"><div className="loader" /> Loading scan detail…</div>;
  if (state.error) return <div className="result-card" role="alert"><Icon name="alert" size={18} /> {state.error}<button className="icon-button" onClick={onClose} aria-label="Close result">×</button></div>;
  const scan = state.scan;
  if (!scan) return null;
  const hasScore = scan.status === "completed" && typeof scan.confidence === "number" && Number.isFinite(scan.confidence) && scan.confidence >= 0 && scan.confidence <= 1;
  const score = hasScore ? Number(scan.confidence) : null;
  const signalLabel = hasScore ? ({ low: "Lower signal", review: "Needs review", high: "Higher signal" }[scan.deepfake_status] || "Scored") : "Inconclusive";
  return <article className="result-card"><div className="result-top"><div><p className="eyebrow">Scan result</p><h2>{scan.filename}</h2><p className="result-meta">Analyzed {formatDate(scan.created_at)} · {formatDuration(scan.duration_seconds)} · {formatBytes(scan.size_bytes)}</p></div><div className="result-actions"><StatusBadge status={scan.status} /><button className="icon-button" onClick={onClose} aria-label="Close result">×</button></div></div><div className="result-body"><div className={`inconclusive-panel ${hasScore ? "scored-panel" : ""}`}><div className="inconclusive-icon"><Icon name={hasScore ? "waveform" : "info"} size={23} /></div><div><strong>{hasScore ? "Probabilistic model indicator" : "No risk score generated"}</strong><p>{scan.explanation || "Inference is unavailable until a validated model is connected."}</p></div></div><div className="result-grid"><div><span>Deepfake signal</span><strong className={hasScore ? "" : "neutral-text"}>{signalLabel}</strong><small>{hasScore ? `${Math.round(score * 100)}% model score` : "No model configured"}</small></div><div><span>Scam language</span><strong className="neutral-text">Unavailable</strong><small>No transcript supplied</small></div><div><span>Model version</span><strong>{scan.model_version || "not-configured"}</strong><small>{hasScore ? "Preprocessing contract matched" : "Waiting for evaluation"}</small></div></div></div><div className="result-footer"><span><Icon name="shield" size={15} /> Indicators are probabilistic evidence, never proof.</span><button className="delete-button" onClick={() => onDelete(scan.id)}><Icon name="trash" size={14} /> Delete record</button></div></article>;
}
