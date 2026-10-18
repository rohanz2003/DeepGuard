import { useEffect, useState } from "react";
import Icon from "./Icon";
import StatusBadge from "./StatusBadge";
import { apiErrorMessage, apiService } from "../services/api";
import { formatBytes, formatDate, formatDuration } from "../utils/format";
import { normalizeScan } from "../utils/scan";

const isProbability = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;

export default function ResultCard({ scanId, initialScan, onDelete, onClose }) {
  const [state, setState] = useState({ scan: initialScan ? normalizeScan(initialScan) : null, loading: Boolean(scanId), error: "" });

  useEffect(() => {
    let active = true;
    const normalizedInitial = initialScan ? normalizeScan(initialScan) : null;
    setState({ scan: normalizedInitial, loading: Boolean(scanId && !normalizedInitial), error: "" });

    // History rows already contain the complete metadata-only scan result. Do
    // not replace a fresh upload result with a second, stale read from an API
    // process that has not been restarted after a schema change.
    if (scanId && !normalizedInitial) {
      apiService.getScan(scanId)
        .then(({ data }) => active && setState({ scan: normalizeScan(data.scan || data), loading: false, error: "" }))
        .catch((error) => active && setState({ scan: null, loading: false, error: apiErrorMessage(error, "Could not load this scan detail.") }));
    }
    return () => { active = false; };
  }, [scanId, initialScan]);

  if (state.loading) return <div className="result-card" role="status"><div className="loader" /> Loading scan detail…</div>;
  if (state.error) return <div className="result-card" role="alert"><Icon name="alert" size={18} /> {state.error}<button className="icon-button" onClick={onClose} aria-label="Close result">×</button></div>;
  const scan = state.scan;
  if (!scan) return null;

  const hasVoiceScore = isProbability(scan.confidence);
  const voiceScore = hasVoiceScore ? Math.round(scan.confidence * 100) : null;
  const voiceStatusAvailable = !["unavailable", "inconclusive"].includes(scan.deepfake_status);
  const voiceStatus = hasVoiceScore || voiceStatusAvailable ? scan.deepfake_status : "inconclusive";
  const scamCategories = Array.isArray(scan.scam_categories) ? scan.scam_categories : [];
  const scamStatusAvailable = !["unavailable", "inconclusive"].includes(scan.scam_status);
  const hasScamScore = scamStatusAvailable && isProbability(scan.scam_score);
  const hasScamSignal = scamStatusAvailable && (hasScamScore || scamCategories.length > 0 || Boolean(scan.scam_explanation) || Boolean(scan.scam_status));
  const scamStatus = hasScamSignal ? scan.scam_status : "unavailable";
  const scamScore = hasScamScore ? Math.round(scan.scam_score * 100) : null;
  const displayedScamCategories = hasScamSignal ? scamCategories : [];
  const displayedScamExplanation = hasScamSignal ? (scan.scam_explanation || "No scam-language explanation was returned.") : "No transcript supplied, so scam-language analysis is unavailable.";

  return <article className="result-card">
    <div className="result-top"><div><p className="eyebrow">Scan result</p><h2>{scan.filename}</h2><p className="result-meta">Analyzed {formatDate(scan.created_at)} · {formatDuration(scan.duration_seconds)} · {formatBytes(scan.size_bytes)}</p></div><div className="result-actions"><StatusBadge status={scan.status} /><button className="icon-button" onClick={onClose} aria-label="Close result">×</button></div></div>
    <div className="result-body">
      <div className={`inconclusive-panel ${hasVoiceScore ? "scored-panel" : ""}`}><div className="inconclusive-icon"><Icon name={hasVoiceScore ? "waveform" : "info"} size={23} /></div><div><strong>{hasVoiceScore ? "Probabilistic voice authenticity indicator" : "No risk score generated"}</strong><p>{scan.explanation || (hasVoiceScore ? "The voice signal is probabilistic evidence and is not proof that a voice is fake." : "Voice authenticity is unavailable until a validated model is connected.")}</p></div></div>
      <div className="signal-grid">
        <section className="signal-card voice-signal-card" aria-labelledby="voice-signal-heading"><div className="signal-card-heading"><div><p className="eyebrow">Voice authenticity</p><h3 id="voice-signal-heading">Voice authenticity signal</h3></div><StatusBadge status={voiceStatus} /></div><div className={`signal-score ${hasVoiceScore ? "" : "unavailable"}`}>{hasVoiceScore ? `${voiceScore}%` : "Unavailable"}</div><p className="signal-caption">{hasVoiceScore ? `${voiceScore}% model score from confidence` : "No voice model score was generated."}</p><p className="signal-explanation">{hasVoiceScore ? "Use this signal as probabilistic evidence for human review." : "The voice signal is unavailable for this scan."}</p></section>
        <section className="signal-card scam-signal-card" aria-labelledby="scam-signal-heading"><div className="signal-card-heading"><div><p className="eyebrow">Scam-language analysis</p><h3 id="scam-signal-heading">Scam-language signal</h3></div><StatusBadge status={scamStatus} /></div><div className={`signal-score ${hasScamScore ? "" : "unavailable"}`}>{hasScamScore ? `${scamScore}%` : "Unavailable"}</div><p className="signal-caption">{hasScamScore ? `${scamScore}% scam-language score` : hasScamSignal ? "Status available; no scam-language score returned." : "No transcript supplied"}</p>{displayedScamCategories.length > 0 && <ul className="signal-categories" aria-label="Scam-language categories">{displayedScamCategories.map((category) => <li key={category}>{category}</li>)}</ul>}<p className="signal-explanation">{displayedScamExplanation}</p></section>
      </div>
      <div className="result-grid"><div><span>Model version</span><strong>{scan.model_version || "not-configured"}</strong><small>{hasVoiceScore ? "Voice preprocessing contract matched" : "Waiting for voice evaluation"}</small></div><div><span>Audio retention</span><strong>Not retained</strong><small>Metadata-only scan record</small></div><div><span>Transcript display</span><strong>Hidden</strong><small>Raw transcript text is never shown</small></div></div>
    </div>
    <div className="result-footer"><span><Icon name="shield" size={15} /> Voice and scam-language signals are probabilistic evidence, never proof.</span><button className="delete-button" onClick={() => onDelete(scan.id)}><Icon name="trash" size={14} /> Delete record</button></div>
  </article>;
}
