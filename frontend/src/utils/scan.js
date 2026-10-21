export const SCAN_POLL_INTERVAL_MS = 1500;

// These fields are input/content fields, not scan metadata. Keep them out of
// every object that can be merged into history or rendered by a result card.
export const SENSITIVE_SCAN_FIELDS = new Set([
  "transcript",
  "transcript_text",
  "raw_transcript",
  "audio",
  "audio_bytes",
  "raw_audio",
  "raw_audio_bytes",
  "audio_data",
  "audio_base64",
  "raw_audio_base64",
  "wav_bytes",
  "audio_storage_id",
]);

const PLAYBACK_URL_FIELDS = ["audio_playback_url", "playback_url", "audio_url"];
const PLAYBACK_ENDPOINT_FIELDS = [
  "audio_playback_endpoint",
  "playback_endpoint",
  "audio_endpoint",
  "audio_playback_path",
  "playback_path",
];

function safePlaybackReference(value) {
  if (typeof value !== "string") return "";
  const reference = value.trim();
  // Data/blob URLs could carry the recording itself. Playback references in
  // scan JSON must be URLs or owner-scoped endpoint paths only.
  if (!reference || /^(data|blob|javascript):/i.test(reference)) return "";
  return reference;
}

export function getPlaybackDescriptor(scan = {}) {
  const source = scan && typeof scan === "object" ? scan : {};
  for (const field of PLAYBACK_URL_FIELDS) {
    const reference = safePlaybackReference(source[field]);
    if (!reference) continue;
    // Relative/non-HTTP references are API routes and need the authenticated
    // apiService blob request. Absolute HTTP URLs are expected to be
    // owner-scoped/signed by the API.
    if (!/^https?:\/\//i.test(reference)) return { type: "endpoint", value: reference };
    return { type: "url", value: reference };
  }
  for (const field of PLAYBACK_ENDPOINT_FIELDS) {
    const reference = safePlaybackReference(source[field]);
    if (reference) return { type: "endpoint", value: reference };
  }
  // The retained-audio API exposes an owner-scoped route rather than putting
  // a signed URL into scan JSON. Keep this endpoint metadata-only and fetch it
  // through apiService so the auth interceptor protects the request.
  if (source.audio_retained === true && source.id) {
    return { type: "endpoint", value: `/scans/${encodeURIComponent(source.id)}/audio` };
  }
  return null;
}

export function isAudioRetained(scan = {}) {
  const source = scan && typeof scan === "object" ? scan : {};
  if (source.audio_retained === true || source.retain_audio === true) return true;
  if (source.audio_retention === "retained" || source.audio_retention === "available") return true;
  return Boolean(getPlaybackDescriptor(source) || source.audio_playback_available === true);
}

export function normalizeScan(scan = {}) {
  const source = scan && typeof scan === "object" ? scan : {};
  const normalized = {
    ...source,
    status: source.status || "unavailable",
    deepfake_status: source.deepfake_status || "inconclusive",
    confidence: source.confidence ?? null,
    scam_status: source.scam_status || "unavailable",
    scam_score: source.scam_score ?? null,
    scam_categories: Array.isArray(source.scam_categories)
      ? source.scam_categories.filter((category) => typeof category === "string" && category.trim())
      : [],
    scam_explanation: source.scam_explanation || "",
    model_version: source.model_version || "not-configured",
    error_code: typeof source.error_code === "string" ? source.error_code : "",
    error_explanation: typeof source.error_explanation === "string" ? source.error_explanation : "",
    expires_at: source.expires_at || null,
  };

  // Audio and transcript content are input-only secrets. Do not carry them
  // into the normalized object used by read-back and result views.
  SENSITIVE_SCAN_FIELDS.forEach((field) => delete normalized[field]);
  return normalized;
}

export function isScanInFlight(status) {
  return status === "pending" || status === "processing";
}

export function mergeScan(previousScan, nextScan) {
  const previous = previousScan && typeof previousScan === "object" ? previousScan : {};
  const next = nextScan && typeof nextScan === "object" ? nextScan : {};
  const merged = { ...previous, ...next };
  if (previous.status && !isScanInFlight(previous.status) && isScanInFlight(next.status)) merged.status = previous.status;
  return normalizeScan(merged);
}

export function normalizeScanResponse(response = {}) {
  return {
    scans: Array.isArray(response.scans) ? response.scans.map(normalizeScan) : [],
    stats: {
      total_scans: 0,
      suspicious_results: 0,
      analyzed_results: 0,
      inconclusive_results: 0,
      ...(response.stats || {}),
    },
  };
}
