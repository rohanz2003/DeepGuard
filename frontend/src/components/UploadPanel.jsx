import { useEffect, useRef, useState } from "react";
import Icon from "./Icon";
import { apiErrorMessage, apiService } from "../services/api";
import { formatBytes } from "../utils/format";

const MAX_BYTES = 25 * 1024 * 1024;
const MAX_TRANSCRIPT_CHARS = 12000;
const TRANSCRIPT_LENGTH_ERROR = `Transcript must be ${MAX_TRANSCRIPT_CHARS} characters or fewer.`;
const TRANSCRIPT_CONSENT_ERROR = "Confirm that you have consent to analyze this transcript for scam-language signals.";
const ALLOWED_AUDIO_EXTENSIONS = [".wav", ".mp3"];
const ALLOWED_AUDIO_MIMES = ["audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave", "audio/mpeg", "audio/mp3", "audio/x-mpeg", "audio/x-mp3", "audio/mpeg3", "audio/x-mpeg-3"];

function uploadStatusMessage(status) {
  if (status === "pending") return "Recording queued for analysis. This result will update automatically.";
  if (status === "processing") return "Recording is processing. This result will update automatically.";
  if (status === "failed") return "Recording analysis failed. Open the result for details.";
  if (status === "unavailable") return "Recording validated, but analysis is unavailable for this scan.";
  if (status === "completed") return "Research analysis completed. Review the separate voice and scam-language signals.";
  return "Recording submitted. We are checking its analysis status.";
}

export default function UploadPanel({ onUploaded, onPreviewDiscard, readOnly = false }) {
  const inputRef = useRef(null);
  const previewUrlRef = useRef("");
  const previewTransferredRef = useRef(false);
  const previewDiscardRef = useRef(onPreviewDiscard);
  const [dragging, setDragging] = useState(false);
  const [file, setFile] = useState(null);
  const [previewUrl, setPreviewUrl] = useState("");
  const [consent, setConsent] = useState(false);
  const [retainAudio, setRetainAudio] = useState(false);
  const [transcript, setTranscript] = useState("");
  const [transcriptConsent, setTranscriptConsent] = useState(false);
  const [state, setState] = useState({ loading: false, error: "", message: "" });
  const hasTranscript = transcript.trim().length > 0;
  const transcriptTooLong = transcript.length > MAX_TRANSCRIPT_CHARS;
  previewDiscardRef.current = onPreviewDiscard;

  useEffect(() => () => {
    // Once a successful upload transfers the URL to the current result, the
    // parent owns its cleanup. Unsubmitted browser previews stay local here.
    if (previewUrlRef.current && !previewTransferredRef.current && typeof URL !== "undefined" && typeof URL.revokeObjectURL === "function") {
      URL.revokeObjectURL(previewUrlRef.current);
    }
  }, []);

  const releasePreview = () => {
    const currentUrl = previewUrlRef.current;
    if (!currentUrl) return;
    if (previewTransferredRef.current) {
      if (previewDiscardRef.current) previewDiscardRef.current(currentUrl);
      else if (typeof URL !== "undefined" && typeof URL.revokeObjectURL === "function") URL.revokeObjectURL(currentUrl);
    } else if (typeof URL !== "undefined" && typeof URL.revokeObjectURL === "function") {
      URL.revokeObjectURL(currentUrl);
    }
    previewUrlRef.current = "";
    previewTransferredRef.current = false;
    setPreviewUrl("");
  };

  const chooseFile = (candidate) => {
    if (!candidate) return;
    const filename = candidate.name.toLowerCase();
    const extension = ALLOWED_AUDIO_EXTENSIONS.find((value) => filename.endsWith(value));
    if (!extension || (candidate.type && !ALLOWED_AUDIO_MIMES.includes(candidate.type.toLowerCase()))) {
      return setState({ loading: false, error: "Choose a valid WAV or MP3 audio file.", message: "" });
    }
    if (candidate.size > MAX_BYTES) {
      return setState({ loading: false, error: "That file is larger than the 25 MB limit.", message: "" });
    }
    releasePreview();
    const nextPreviewUrl = typeof URL !== "undefined" && typeof URL.createObjectURL === "function" ? URL.createObjectURL(candidate) : "";
    previewUrlRef.current = nextPreviewUrl;
    setFile(candidate);
    setPreviewUrl(nextPreviewUrl);
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
    if (!file) return setState({ loading: false, error: "Choose a WAV or MP3 file first.", message: "" });
    if (!consent) return setState({ loading: false, error: "Confirm that you have consent to submit this recording.", message: "" });
    if (transcriptTooLong) return setState({ loading: false, error: TRANSCRIPT_LENGTH_ERROR, message: "" });
    if (hasTranscript && !transcriptConsent) return setState({ loading: false, error: TRANSCRIPT_CONSENT_ERROR, message: "" });

    setState({ loading: true, error: "", message: "Uploading recording…" });
    try {
      const uploadedPreview = previewUrl ? { file, url: previewUrl } : null;
      const { data } = await apiService.uploadScan(file, transcript, hasTranscript && transcriptConsent, retainAudio);
      // Transfer the browser-only object URL to the current result before
      // clearing this form's local reference. It never enters scan metadata.
      if (uploadedPreview) previewTransferredRef.current = true;
      if (uploadedPreview) onUploaded?.(data.scan, uploadedPreview);
      else onUploaded?.(data.scan);
      setFile(null);
      setPreviewUrl("");
      previewUrlRef.current = "";
      previewTransferredRef.current = false;
      setConsent(false);
      setRetainAudio(false);
      setTranscript("");
      setTranscriptConsent(false);
      if (inputRef.current) inputRef.current.value = "";
      setState({ loading: false, error: "", message: uploadStatusMessage(data.scan?.status) });
    } catch (error) {
      setState({ loading: false, error: apiErrorMessage(error, "The upload could not be validated."), message: "" });
    }
  };

  return <article className="card upload-card" id="upload">
    <div className="card-heading"><div><p className="eyebrow">New analysis</p><h2>Upload a recording</h2></div><span className="soft-badge"><Icon name="lock" size={13} /> Private</span></div>
     <p className="card-copy">Validate a suspicious voice message with optional owner-protected playback.</p>
    <div className={`drop-zone ${dragging ? "dragging" : ""} ${file ? "has-file" : ""} ${readOnly ? "disabled" : ""}`} onDragOver={(event) => { event.preventDefault(); if (!readOnly) setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={(event) => { event.preventDefault(); setDragging(false); if (!readOnly) chooseFile(event.dataTransfer.files[0]); }} onClick={() => !readOnly && inputRef.current?.click()} role="button" tabIndex={readOnly ? -1 : 0} aria-disabled={readOnly} onKeyDown={(event) => { if (!readOnly && (event.key === "Enter" || event.key === " ")) inputRef.current?.click(); }}>
        <label htmlFor="audio-upload" className="sr-only">WAV or MP3 audio file</label>
        <input id="audio-upload" ref={inputRef} type="file" accept=".wav,.mp3,audio/wav,audio/x-wav,audio/wave,audio/vnd.wave,audio/mpeg,audio/mp3,audio/x-mpeg,audio/x-mp3,audio/mpeg3,audio/x-mpeg-3" hidden disabled={readOnly} onChange={(event) => chooseFile(event.target.files[0])} />
        {readOnly ? <><div className="drop-icon"><Icon name="lock" size={21} /></div><strong>Preview workspace is read-only</strong><span>Sign in to validate a recording</span></> : file ? <><div className="drop-icon selected"><Icon name="check" size={21} /></div><strong>{file.name}</strong><span>{formatBytes(file.size)} · Ready to validate</span></> : <><div className="drop-icon"><Icon name="upload" size={21} /></div><strong>Drop a WAV or MP3 recording here</strong><span>or click to browse · up to 25 MB</span></>}
     </div>
      {previewUrl && <div className="audio-preview"><div className="audio-preview-heading"><span><Icon name="waveform" size={15} /> Browser-only preview</span><small>Nothing is uploaded by previewing</small></div><audio controls preload="metadata" src={previewUrl} aria-label="Audio recording preview" /></div>}
     {!readOnly && <>
      <div className="transcript-field">
        <div className="transcript-field-heading"><label className="transcript-label" htmlFor="transcript-input">Transcript <span>(optional)</span></label><span className={`transcript-counter ${transcriptTooLong ? "over-limit" : ""}`} id="transcript-counter" aria-live="polite">{transcript.length} / {MAX_TRANSCRIPT_CHARS} characters</span></div>
        <textarea id="transcript-input" className="transcript-input" rows="5" maxLength={MAX_TRANSCRIPT_CHARS} value={transcript} onChange={updateTranscript} placeholder="Paste the spoken words to check for scam-language signals." aria-invalid={transcriptTooLong} aria-describedby={transcriptTooLong ? "transcript-counter transcript-error" : "transcript-counter"} />
        {transcriptTooLong && <p className="inline-error transcript-error" id="transcript-error" role="alert"><Icon name="alert" size={15} />{TRANSCRIPT_LENGTH_ERROR}</p>}
        {hasTranscript && <label className="consent-row transcript-consent-row" htmlFor="transcript-consent"><input id="transcript-consent" type="checkbox" required aria-required="true" checked={transcriptConsent} onChange={(event) => setTranscriptConsent(event.target.checked)} /> <span>I have consent to analyze this transcript for scam-language signals.</span></label>}
       </div>
       <label className="consent-row" htmlFor="audio-consent"><input id="audio-consent" type="checkbox" required aria-required="true" checked={consent} onChange={(event) => setConsent(event.target.checked)} /> <span>I have consent to submit this recording for review.</span></label>
       <label className="consent-row" htmlFor="audio-retention"><input id="audio-retention" type="checkbox" checked={retainAudio} onChange={(event) => setRetainAudio(event.target.checked)} /> <span>Keep this recording available for playback for 30 days</span></label>
       <p className="retention-copy">Audio is processed in memory by default; only private scan metadata, including the filename, is retained for 30 days. If selected, the original recording remains owner-protected for playback for 30 days. Transcript text is not shown after upload.</p>
     </>}
    {state.error && <div className="inline-error" role="alert" aria-live="assertive"><Icon name="alert" size={15} />{state.error}</div>}
    {state.message && <div className="inline-success" role="status" aria-live="polite">{state.message}</div>}
     <div className="upload-footer"><span><Icon name="info" size={14} /> WAV or MP3 · max 25 MB</span>{!readOnly && <button type="button" className="button primary" onClick={(event) => { event.stopPropagation(); submit(); }} disabled={state.loading} aria-busy={state.loading}>{state.loading ? <><span className="button-spinner" /> Validating…</> : <>Start review <Icon name="arrow" size={16} /></>}</button>}</div>
  </article>;
}
