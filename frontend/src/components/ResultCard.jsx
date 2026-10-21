import { useEffect, useMemo, useRef, useState } from "react";
import Icon from "./Icon";
import StatusBadge from "./StatusBadge";
import { apiErrorMessage, apiService } from "../services/api";
import { formatBytes, formatDate, formatDuration } from "../utils/format";
import { getPlaybackDescriptor, isAudioRetained, isScanInFlight, mergeScan, normalizeScan, SCAN_POLL_INTERVAL_MS } from "../utils/scan";

const isProbability = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;
const PLAYBACK_RETRY_LIMIT = 3;

function isTransientPlaybackError(error) {
  return !error?.response && (
    error?.code === "ERR_NETWORK"
    || error?.code === "ECONNABORTED"
    || error?.message === "Network Error"
  );
}

export default function ResultCard({ scanId, initialScan, onDelete, onClose, onScanUpdate, preview = null }) {
  const [state, setState] = useState({ scan: initialScan ? normalizeScan(initialScan) : null, loading: Boolean(scanId), error: "" });
  const [playbackState, setPlaybackState] = useState({ status: "unavailable", src: "", source: "", error: "" });
  const initialScanRef = useRef(initialScan);
  const scanRef = useRef(state.scan);
  const pollTimerRef = useRef(null);
  const playbackObjectUrlRef = useRef("");
  const playbackRetryTimerRef = useRef(null);
  const playbackAttemptRef = useRef(0);
  const playbackDescriptorKeyRef = useRef("");
  const [playbackRetryNonce, setPlaybackRetryNonce] = useState(0);
  initialScanRef.current = initialScan;

  useEffect(() => {
    let active = true;
    const normalizedInitial = initialScanRef.current ? normalizeScan(initialScanRef.current) : null;
    scanRef.current = normalizedInitial;
    setState({ scan: normalizedInitial, loading: Boolean(scanId && !normalizedInitial), error: "" });

    // History rows already contain metadata-only scan results. Refresh an
    // in-flight row, while retaining the fresh upload fields during polling.
    if (scanId && (!normalizedInitial || isScanInFlight(normalizedInitial.status))) {
      const fetchScan = () => {
        pollTimerRef.current = null;
        apiService.getScan(scanId)
          .then(({ data }) => {
            if (!active) return;
            const nextScan = mergeScan(scanRef.current, data?.scan || data);
            scanRef.current = nextScan;
            setState({ scan: nextScan, loading: false, error: "" });
            onScanUpdate?.(nextScan);
            if (isScanInFlight(nextScan.status)) pollTimerRef.current = window.setTimeout(fetchScan, SCAN_POLL_INTERVAL_MS);
          })
          .catch((error) => active && setState((previous) => ({ ...previous, loading: false, error: apiErrorMessage(error, "Could not load this scan detail.") })));
      };
      fetchScan();
    }
    return () => {
      active = false;
      if (pollTimerRef.current) window.clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    };
  }, [scanId, onScanUpdate]);

  useEffect(() => {
    if (!scanId || !initialScan) return;
    const incoming = normalizeScan(initialScan);
    const current = scanRef.current;
    if (!current || current.id !== incoming.id || !isScanInFlight(current.status) || isScanInFlight(incoming.status)) return;
    const merged = mergeScan(current, incoming);
    scanRef.current = merged;
    setState({ scan: merged, loading: false, error: "" });
    if (pollTimerRef.current) window.clearTimeout(pollTimerRef.current);
    pollTimerRef.current = null;
  }, [initialScan, scanId]);

  const scan = state.scan;
  const audioEndpoint = scan?.audio_endpoint;
  const audioPlaybackEndpoint = scan?.audio_playback_endpoint;
  const audioPlaybackUrl = scan?.audio_playback_url;
  const audioUrl = scan?.audio_url;
  const playbackEndpoint = scan?.playback_endpoint;
  const playbackPath = scan?.playback_path;
  const playbackUrl = scan?.playback_url;
  const audioRetainedFlag = scan?.audio_retained;
  const scanIdentifier = scan?.id;
  const playbackDescriptor = useMemo(() => (
    preview?.url
      ? { type: "url", value: preview.url }
      : getPlaybackDescriptor({ audio_endpoint: audioEndpoint, audio_playback_endpoint: audioPlaybackEndpoint, audio_playback_url: audioPlaybackUrl, audio_retained: audioRetainedFlag, audio_url: audioUrl, id: scanIdentifier, playback_endpoint: playbackEndpoint, playback_path: playbackPath, playback_url: playbackUrl })
  ), [audioEndpoint, audioPlaybackEndpoint, audioPlaybackUrl, audioRetainedFlag, audioUrl, playbackEndpoint, playbackPath, playbackUrl, preview?.url, scanIdentifier]);
  useEffect(() => {
    let active = true;
    const previousObjectUrl = playbackObjectUrlRef.current;
    if (previousObjectUrl && typeof URL !== "undefined" && typeof URL.revokeObjectURL === "function") URL.revokeObjectURL(previousObjectUrl);
    playbackObjectUrlRef.current = "";

    const descriptor = playbackDescriptor;
    const descriptorKey = descriptor ? `${descriptor.type}:${descriptor.value}` : "";
    if (playbackDescriptorKeyRef.current !== descriptorKey) {
      playbackDescriptorKeyRef.current = descriptorKey;
      playbackAttemptRef.current = 0;
    }
    if (playbackRetryTimerRef.current) window.clearTimeout(playbackRetryTimerRef.current);
    playbackRetryTimerRef.current = null;
    if (!descriptor) {
      setPlaybackState({ status: "unavailable", src: "", source: "", error: "" });
      return () => { active = false; };
    }
    if (descriptor.type === "url") {
      setPlaybackState({ status: "available", src: descriptor.value, source: preview?.url ? "local" : "api", error: "" });
      return () => { active = false; };
    }

    const attempt = playbackAttemptRef.current + 1;
    playbackAttemptRef.current = attempt;
    setPlaybackState({ status: "loading", src: "", source: "api", error: "" });
    apiService.getScanPlayback(scan?.id, descriptor.value)
      .then(({ data }) => {
        if (!active) return;
        if (typeof Blob === "undefined" || !(data instanceof Blob) || typeof URL === "undefined" || typeof URL.createObjectURL !== "function") {
          throw new Error("PLAYBACK_RESPONSE_INVALID");
        }
        const objectUrl = URL.createObjectURL(data);
        playbackObjectUrlRef.current = objectUrl;
        setPlaybackState({ status: "available", src: objectUrl, source: "api", error: "" });
      })
      .catch((error) => {
        if (!active) return;
        if (isTransientPlaybackError(error) && attempt < PLAYBACK_RETRY_LIMIT) {
          playbackRetryTimerRef.current = window.setTimeout(() => {
            playbackRetryTimerRef.current = null;
            if (active) setPlaybackRetryNonce((value) => value + 1);
          }, attempt * 1000);
          return;
        }
        setPlaybackState({ status: "error", src: "", source: "api", error: apiErrorMessage(error, "Playback is unavailable for this scan.") });
      });
    return () => {
      active = false;
      if (playbackRetryTimerRef.current) window.clearTimeout(playbackRetryTimerRef.current);
      playbackRetryTimerRef.current = null;
      const objectUrl = playbackObjectUrlRef.current;
      if (objectUrl && typeof URL !== "undefined" && typeof URL.revokeObjectURL === "function") URL.revokeObjectURL(objectUrl);
      playbackObjectUrlRef.current = "";
    };
  }, [playbackDescriptor, playbackRetryNonce, preview?.url, scan?.id]);

  const retryPlayback = () => {
    playbackAttemptRef.current = 0;
    setPlaybackRetryNonce((value) => value + 1);
  };

  if (state.loading) return <div className="result-card" role="status"><div className="loader" /> Loading scan detail…</div>;
  if (state.error) return <div className="result-card" role="alert"><Icon name="alert" size={18} /> {state.error}<button className="icon-button" onClick={onClose} aria-label="Close result">×</button></div>;
  if (!scan) return null;

  const failed = scan.status === "failed";
  const inFlight = isScanInFlight(scan.status);
  const inFlightHeading = scan.status === "pending" ? "Analysis queued" : "Analysis in progress";
  const inFlightExplanation = scan.explanation || (scan.status === "pending" ? "The recording is queued for voice analysis." : "The recording is being analyzed. This result will update automatically.");
  const failureExplanation = typeof scan.error_explanation === "string" && scan.error_explanation.trim() ? scan.error_explanation : "The scan could not be completed.";
  const failureCode = typeof scan.error_code === "string" && scan.error_code.trim() ? scan.error_code : "";
  const hasVoiceScore = isProbability(scan.confidence);
  const voiceScore = hasVoiceScore ? Math.round(scan.confidence * 100) : null;
  const voiceStatusAvailable = !["unavailable", "inconclusive"].includes(scan.deepfake_status);
  const voiceStatus = inFlight ? scan.status : hasVoiceScore || voiceStatusAvailable ? scan.deepfake_status : "inconclusive";
  const scamCategories = Array.isArray(scan.scam_categories) ? scan.scam_categories : [];
  const scamStatusAvailable = !["unavailable", "inconclusive"].includes(scan.scam_status);
  const hasScamScore = scamStatusAvailable && isProbability(scan.scam_score);
  const hasScamSignal = scamStatusAvailable && (hasScamScore || scamCategories.length > 0 || Boolean(scan.scam_explanation) || Boolean(scan.scam_status));
  const scamStatus = hasScamSignal ? scan.scam_status : "unavailable";
  const scamScore = hasScamScore ? Math.round(scan.scam_score * 100) : null;
  const displayedScamCategories = hasScamSignal ? scamCategories : [];
  const displayedScamExplanation = hasScamSignal ? (scan.scam_explanation || "No scam-language explanation was returned.") : "No transcript supplied, so scam-language analysis is unavailable.";
  const audioRetained = isAudioRetained(scan);
  const audioRetentionDays = Number.isFinite(scan.audio_retention_days) ? scan.audio_retention_days : 30;
  const playbackAvailable = playbackState.status === "available";
  const browserPreviewAvailable = playbackAvailable && playbackState.source === "local";
  const playbackHeading = playbackAvailable
    ? browserPreviewAvailable ? "Browser preview available" : "Playback available"
    : playbackState.status === "loading"
      ? "Loading private playback"
      : playbackState.status === "error"
        ? "Playback unavailable"
      : audioRetained
        ? "Recording retained for playback"
        : "Playback not retained";
  const playbackExplanation = playbackAvailable
    ? browserPreviewAvailable ? "This browser-only preview is available for the current result; it is not stored in scan metadata." : "Owner-protected audio is available for this scan."
    : playbackState.status === "loading"
      ? "Requesting the owner-scoped playback endpoint."
      : playbackState.status === "error"
        ? "The owner-scoped playback request could not be completed."
      : audioRetained
        ? "The API retained this recording, but has not returned a playback reference yet."
        : "The original recording was not retained, so playback is unavailable.";

  return <article className="result-card">
    <div className="result-top"><div><p className="eyebrow">Scan result</p><h2>{scan.filename}</h2><p className="result-meta">Analyzed {formatDate(scan.created_at)} · {formatDuration(scan.duration_seconds)} · {formatBytes(scan.size_bytes)}</p></div><div className="result-actions"><StatusBadge status={scan.status} /><button className="icon-button" onClick={onClose} aria-label="Close result">×</button></div></div>
    <div className="result-body">
       <div className={`inconclusive-panel ${hasVoiceScore ? "scored-panel" : ""} ${failed ? "failure-panel" : ""}`} role={failed ? "alert" : undefined}><div className="inconclusive-icon"><Icon name={failed ? "alert" : hasVoiceScore ? "waveform" : "info"} size={23} /></div><div><strong>{failed ? "Analysis failed" : inFlight ? inFlightHeading : hasVoiceScore ? "Probabilistic voice authenticity indicator" : "No risk score generated"}</strong><p>{failed ? failureExplanation : inFlight ? inFlightExplanation : scan.explanation || (hasVoiceScore ? "The voice signal is probabilistic evidence and is not proof that a voice is fake." : "Voice authenticity is unavailable until a validated model is connected.")}</p>{failed && failureCode && <small className="failure-code">Error code: {failureCode}</small>}</div></div>
       <div className="signal-grid">
          <section className="signal-card voice-signal-card" aria-labelledby="voice-signal-heading"><div className="signal-card-heading"><div><p className="eyebrow">Voice authenticity</p><h3 id="voice-signal-heading">Voice authenticity signal</h3></div><StatusBadge status={voiceStatus} /></div><div className={`signal-score ${hasVoiceScore ? "" : "unavailable"}`}>{hasVoiceScore ? `${voiceScore}%` : "Unavailable"}</div><p className="signal-caption">{hasVoiceScore ? `${voiceScore}% model score from confidence` : inFlight ? scan.status === "pending" ? "Voice analysis is queued." : "Voice analysis is processing." : "No voice model score was generated."}</p><p className="signal-explanation">{hasVoiceScore ? "Use this signal as probabilistic evidence for human review." : inFlight ? "The voice signal will appear when processing finishes." : "The voice signal is unavailable for this scan."}</p></section>
         <section className="signal-card scam-signal-card" aria-labelledby="scam-signal-heading"><div className="signal-card-heading"><div><p className="eyebrow">Scam-language analysis</p><h3 id="scam-signal-heading">Scam-language signal</h3></div><StatusBadge status={scamStatus} /></div><div className={`signal-score ${hasScamScore ? "" : "unavailable"}`}>{hasScamScore ? `${scamScore}%` : "Unavailable"}</div><p className="signal-caption">{hasScamScore ? `${scamScore}% scam-language score` : hasScamSignal ? "Status available; no scam-language score returned." : "No transcript supplied"}</p>{displayedScamCategories.length > 0 && <ul className="signal-categories" aria-label="Scam-language categories">{displayedScamCategories.map((category) => <li key={category}>{category}</li>)}</ul>}<p className="signal-explanation">{displayedScamExplanation}</p></section>
       </div>
        <section className={`result-playback ${playbackAvailable ? "" : "unavailable"}`} aria-label="Audio playback"><div className="result-playback-heading"><span className="result-playback-icon"><Icon name={playbackAvailable ? "waveform" : audioRetained ? "lock" : "info"} size={17} /></span><div><h3>{playbackHeading}</h3><p>{playbackExplanation}</p></div></div>{playbackAvailable && <audio controls preload="metadata" src={playbackState.src} aria-label={browserPreviewAvailable ? "Audio recording preview" : "Private audio playback"} controlsList="nodownload" />}{playbackState.status === "error" && <div className="playback-error"><small role="status">{playbackState.error}</small><button type="button" className="text-button" onClick={retryPlayback}>Retry playback</button></div>}</section>
       <div className="result-grid"><div><span>Model version</span><strong>{scan.model_version || "not-configured"}</strong><small>{hasVoiceScore ? "Voice preprocessing contract matched" : "Waiting for voice evaluation"}</small></div><div><span>Audio retention</span><strong>{audioRetained ? `Retained for ${audioRetentionDays} days` : "Not retained"}</strong><small>{browserPreviewAvailable ? audioRetained ? `Browser preview; server retention is ${audioRetentionDays} days` : "Browser-only preview; not retained" : playbackAvailable ? "Owner-protected playback available" : audioRetained ? "Waiting for a playback reference" : "Metadata-only scan record"}</small></div><div><span>Transcript display</span><strong>Hidden</strong><small>Raw transcript text is never shown</small></div></div>
    </div>
    <div className="result-footer"><span><Icon name="shield" size={15} /> Voice and scam-language signals are probabilistic evidence, never proof.</span><button className="delete-button" onClick={() => onDelete(scan.id)}><Icon name="trash" size={14} /> Delete record</button></div>
  </article>;
}
