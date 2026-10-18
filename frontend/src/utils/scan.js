export const SCAN_POLL_INTERVAL_MS = 1500;

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

  // Transcript text is an input-only secret. Do not carry it into the
  // normalized object used by read-back and result views.
  delete normalized.transcript;
  delete normalized.transcript_text;
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
