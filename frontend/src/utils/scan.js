export function normalizeScan(scan = {}) {
  return {
    ...scan,
    status: scan.status || "unavailable",
    confidence: scan.confidence ?? null,
    model_version: scan.model_version || "not-configured",
    expires_at: scan.expires_at || null,
  };
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
