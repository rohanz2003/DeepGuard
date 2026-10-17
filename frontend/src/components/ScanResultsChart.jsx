export default function ScanResultsChart({ stats }) {
  const suspicious = stats?.suspicious_results || 0;
  const analyzed = stats?.analyzed_results || 0;
  const values = [suspicious, Math.max(0, analyzed - suspicious), stats?.inconclusive_results || 0];
  const total = values.reduce((sum, value) => sum + value, 0);
  if (!total) return <article className="card scan-chart empty-chart"><p className="eyebrow">Result distribution</p><h2>No stored result counts yet</h2><p>Counts will appear after the API returns scan records. No sample data is shown.</p></article>;
  const max = Math.max(...values, 1);
  return <article className="card scan-chart"><div className="card-heading"><div><p className="eyebrow">Result distribution</p><h2>Stored scan outcomes</h2></div><span className="muted-cell">{total} records</span></div><div className="chart-bars" role="img" aria-label={`${values[0]} needs review, ${values[1]} lower signal, and ${values[2]} inconclusive stored scan outcomes`}>{[["suspicious", "Needs review"], ["lower", "Lower signal"], ["inconclusive", "Inconclusive"]].map(([tone, label], index) => <div className="chart-bar-group" key={tone}><div className={`chart-bar ${tone}`} style={{ height: `${Math.max((values[index] / max) * 100, 6)}%` }} /><span>{label}</span><strong>{values[index]}</strong></div>)}</div></article>;
}
