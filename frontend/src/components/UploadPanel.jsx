import { useRef, useState } from "react";
import Icon from "./Icon";
import { apiErrorMessage, apiService } from "../services/api";
import { formatBytes } from "../utils/format";

const MAX_BYTES = 25 * 1024 * 1024;
const MAX_TRANSCRIPT_CHARS = 12000;
const TRANSCRIPT_LENGTH_ERROR = `Transcript must be ${MAX_TRANSCRIPT_CHARS} characters or fewer.`;
const TRANSCRIPT_CONSENT_ERROR = "Confirm that you have consent to analyze this transcript for scam-language signals.";

function uploadStatusMessage(status) {
  if (status === "pending") return "Recording queued for analysis. This result will update automatically.";
  if (status === "processing") return "Recording is processing. This result will update automatically.";
  if (status === "failed") return "Recording analysis failed. Open the result for details.";
  if (status === "unavailable") return "Recording validated, but analysis is unavailable for this scan.";
  if (status === "completed") return "Research analysis completed. Review the separate voice and scam-language signals.";
  return "Recording submitted. We are checking its analysis status.";
}

export default function UploadPanel({ onUploaded, readOnly = false }) {
  const inputRef = useRef(null);
  const [dragging, setDragging] = useState(false);
  const [file, setFile] = useState(null);
  const [consent, setConsent] = useState(false);
  const [transcript, setTranscript] = useState("");
  const [transcriptConsent, setTranscriptConsent] = useState(false);
  const [state, setState] = useState({ loading: false, error: "", message: "" });
  const hasTranscript = transcript.trim().length > 0;
  const transcriptTooLong = transcript.length > MAX_TRANSCRIPT_CHARS;

  const chooseFile = (candidate) => {
    if (!candidate) return;
    if (!candidate.name.toLowerCase().endsWith(".wav") || (candidate.type && !["audio/wav", "audio/x-wav", "audio/wave"].includes(candidate.type))) {
      return setState({ loading: false, error: "Choose a valid WAV audio file.", message: "" });
    }
    if (candidate.size > MAX_BYTES) {
      return setState({ loading: false, error: "That file is larger than the 25 MB limit.", message: "" });
    }
    setFile(candidate);
    setState({ loading: false, error: "", message: "" });
  };

  const updateTranscript = (event) => {
    const value = event.target.value;
    setTranscript(value);
    if (!value.trim()) setTranscriptConsent(false);
    setState((previous) => ({ ...previous, error: previous.error === TRANSCRIPT_LENGTH_ERROR ? "" : previous.error }));
  };

  const submit = async () => {
    if (state.loading) return;
    if (!file) return setState({ loading: false, error: "Choose a WAV file first.", message: "" });
    if (!consent) return setState({ loading: false, error: "Confirm that you have consent to submit this recording.", message: "" });
    if (transcriptTooLong) return setState({ loading: false, error: TRANSCRIPT_LENGTH_ERROR, message: "" });
    if (hasTranscript && !transcriptConsent) return setState({ loading: false, error: TRANSCRIPT_CONSENT_ERROR, message: "" });

    setState({ loading: true, error: "", message: "Uploading recording…" });
    try {
      const { data } = await apiService.uploadScan(file, transcript, hasTranscript && transcriptConsent);
      setFile(null);
      setConsent(false);
      setTranscript("");
      setTranscriptConsent(false);
      if (inputRef.current) inputRef.current.value = "";
      setState({ loading: false, error: "", message: uploadStatusMessage(data.scan?.status) });
      onUploaded(data.scan);
    } catch (error) {
      setState({ loading: false, error: apiErrorMessage(error, "The upload could not be validated."), message: "" });
    }
  };

  return <article className="card upload-card" id="upload">
    <div className="card-heading"><div><p className="eyebrow">New analysis</p><h2>Upload a recording</h2></div><span className="soft-badge"><Icon name="lock" size={13} /> Private</span></div>
    <p className="card-copy">Validate a suspicious voice message without keeping the original audio.</p>
    <div className={`drop-zone ${dragging ? "dragging" : ""} ${file ? "has-file" : ""} ${readOnly ? "disabled" : ""}`} onDragOver={(event) => { event.preventDefault(); if (!readOnly) setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={(event) => { event.preventDefault(); setDragging(false); if (!readOnly) chooseFile(event.dataTransfer.files[0]); }} onClick={() => !readOnly && inputRef.current?.click()} role="button" tabIndex={readOnly ? -1 : 0} aria-disabled={readOnly} onKeyDown={(event) => { if (!readOnly && (event.key === "Enter" || event.key === " ")) inputRef.current?.click(); }}>
      <label htmlFor="audio-upload" className="sr-only">WAV audio file</label>
      <input id="audio-upload" ref={inputRef} type="file" accept=".wav,audio/wav,audio/x-wav" hidden disabled={readOnly} onChange={(event) => chooseFile(event.target.files[0])} />
      {readOnly ? <><div className="drop-icon"><Icon name="lock" size={21} /></div><strong>Preview workspace is read-only</strong><span>Sign in to validate a recording</span></> : file ? <><div className="drop-icon selected"><Icon name="check" size={21} /></div><strong>{file.name}</strong><span>{formatBytes(file.size)} · Ready to validate</span></> : <><div className="drop-icon"><Icon name="upload" size={21} /></div><strong>Drop a WAV recording here</strong><span>or click to browse · up to 25 MB</span></>}
    </div>
    {!readOnly && <>
      <div className="transcript-field">
        <div className="transcript-field-heading"><label className="transcript-label" htmlFor="transcript-input">Transcript <span>(optional)</span></label><span className={`transcript-counter ${transcriptTooLong ? "over-limit" : ""}`} id="transcript-counter" aria-live="polite">{transcript.length} / {MAX_TRANSCRIPT_CHARS} characters</span></div>
        <textarea id="transcript-input" className="transcript-input" rows="5" maxLength={MAX_TRANSCRIPT_CHARS} value={transcript} onChange={updateTranscript} placeholder="Paste the spoken words to check for scam-language signals." aria-invalid={transcriptTooLong} aria-describedby={transcriptTooLong ? "transcript-counter transcript-error" : "transcript-counter"} />
        {transcriptTooLong && <p className="inline-error transcript-error" id="transcript-error" role="alert"><Icon name="alert" size={15} />{TRANSCRIPT_LENGTH_ERROR}</p>}
        {hasTranscript && <label className="consent-row transcript-consent-row" htmlFor="transcript-consent"><input id="transcript-consent" type="checkbox" required aria-required="true" checked={transcriptConsent} onChange={(event) => setTranscriptConsent(event.target.checked)} /> <span>I have consent to analyze this transcript for scam-language signals.</span></label>}
      </div>
      <label className="consent-row" htmlFor="audio-consent"><input id="audio-consent" type="checkbox" required aria-required="true" checked={consent} onChange={(event) => setConsent(event.target.checked)} /> <span>I have consent to submit this recording for review.</span></label>
      <p className="retention-copy">Audio is processed in memory; only private scan metadata, including the filename, is retained for 30 days. Transcript text is not shown after upload.</p>
    </>}
    {state.error && <div className="inline-error" role="alert" aria-live="assertive"><Icon name="alert" size={15} />{state.error}</div>}
    {state.message && <div className="inline-success" role="status" aria-live="polite">{state.message}</div>}
    <div className="upload-footer"><span><Icon name="info" size={14} /> WAV · max 25 MB</span>{!readOnly && <button type="button" className="button primary" onClick={(event) => { event.stopPropagation(); submit(); }} disabled={state.loading} aria-busy={state.loading}>{state.loading ? <><span className="button-spinner" /> Validating…</> : <>Start review <Icon name="arrow" size={16} /></>}</button>}</div>
  </article>;
}
