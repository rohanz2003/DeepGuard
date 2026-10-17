export default function StatusBadge({ status }) {
  const known = { unavailable: ["Inconclusive", "neutral"], completed: ["Analyzed", "success"], pending: ["Pending", "warning"] };
  const [label, tone] = known[status] || [status || "Unknown", "neutral"];
  return <span className={`status-badge ${tone}`}><span className="status-dot" /> {label}</span>;
}
