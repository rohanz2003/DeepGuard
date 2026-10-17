import { useRef, useState } from "react";
import Icon from "./Icon";
import { apiErrorMessage, apiService } from "../services/api";
import { formatBytes } from "../utils/format";

const MAX_BYTES = 25 * 1024 * 1024;
export default function UploadPanel({ onUploaded, readOnly = false }) {
  const inputRef = useRef(null);
  const [dragging, setDragging] = useState(false);
  const [file, setFile] = useState(null);
  const [consent, setConsent] = useState(false);
  const [state, setState] = useState({ loading: false, error: "", message: "" });
  const chooseFile = (candidate) => {
    if (!candidate) return;
    if (!candidate.name.toLowerCase().endsWith(".wav") || (candidate.type && !["audio/wav", "audio/x-wav", "audio/wave"].includes(candidate.type))) return setState({ loading: false, error: "Choose a valid WAV audio file.", message: "" });
    if (candidate.size > MAX_BYTES) return setState({ loading: false, error: "That file is larger than the 25 MB limit.", message: "" });
    setFile(candidate); setState({ loading: false, error: "", message: "" });
  };
  const submit = async () => {
    if (!file) return setState({ loading: false, error: "Choose a WAV file first.", message: "" });
    if (!consent) return setState({ loading: false, error: "Confirm that you have consent to submit this recording.", message: "" });
    setState({ loading: true, error: "", message: "Uploading recording…" });
    try { const { data } = await apiService.uploadScan(file); setFile(null); setConsent(false); if (inputRef.current) inputRef.current.value = ""; setState({ loading: false, error: "", message: data.scan?.status === "completed" ? "Research analysis completed. Review the score and its limitations." : "Recording validated. No model score is available for this scan." }); onUploaded(data.scan); }
    catch (error) { setState({ loading: false, error: apiErrorMessage(error, "The upload could not be validated."), message: "" }); }
  };
  return <article className="card upload-card" id="upload"><div className="card-heading"><div><p className="eyebrow">New analysis</p><h2>Upload a recording</h2></div><span className="soft-badge"><Icon name="lock" size={13} /> Private</span></div><p className="card-copy">Validate a suspicious voice message without keeping the original audio.</p><div className={`drop-zone ${dragging ? "dragging" : ""} ${file ? "has-file" : ""} ${readOnly ? "disabled" : ""}`} onDragOver={(event) => { event.preventDefault(); if (!readOnly) setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={(event) => { event.preventDefault(); setDragging(false); if (!readOnly) chooseFile(event.dataTransfer.files[0]); }} onClick={() => !readOnly && inputRef.current?.click()} role="button" tabIndex={readOnly ? -1 : 0} aria-disabled={readOnly} onKeyDown={(event) => { if (!readOnly && (event.key === "Enter" || event.key === " ")) inputRef.current?.click(); }}><label htmlFor="audio-upload" className="sr-only">WAV audio file</label><input id="audio-upload" ref={inputRef} type="file" accept=".wav,audio/wav,audio/x-wav" hidden disabled={readOnly} onChange={(event) => chooseFile(event.target.files[0])} />{readOnly ? <><div className="drop-icon"><Icon name="lock" size={21} /></div><strong>Preview workspace is read-only</strong><span>Sign in to validate a recording</span></> : file ? <><div className="drop-icon selected"><Icon name="check" size={21} /></div><strong>{file.name}</strong><span>{formatBytes(file.size)} · Ready to validate</span></> : <><div className="drop-icon"><Icon name="upload" size={21} /></div><strong>Drop a WAV recording here</strong><span>or click to browse · up to 25 MB</span></>}</div>{!readOnly && <><label className="consent-row"><input type="checkbox" required checked={consent} onChange={(event) => setConsent(event.target.checked)} /> <span>I have consent to submit this recording for review.</span></label><p className="retention-copy">Audio is processed in memory; only private scan metadata, including the filename, is retained for 30 days.</p></>}{state.error && <div className="inline-error" role="alert"><Icon name="alert" size={15} />{state.error}</div>}{state.message && <div className="inline-success" role="status">{state.message}</div>}<div className="upload-footer"><span><Icon name="info" size={14} /> WAV · max 25 MB</span>{!readOnly && <button className="button primary" onClick={(event) => { event.stopPropagation(); submit(); }} disabled={state.loading}>{state.loading ? <><span className="button-spinner" /> Validating…</> : <>Start review <Icon name="arrow" size={16} /></>}</button>}</div></article>;
}
