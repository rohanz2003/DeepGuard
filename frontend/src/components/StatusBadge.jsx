export default function StatusBadge({ status }) {
  const known = {
    unavailable: ["Unavailable", "neutral"],
    inconclusive: ["Inconclusive", "neutral"],
    completed: ["Analyzed", "success"],
    complete: ["Analyzed", "success"],
    pending: ["Pending", "warning"],
    low: ["Lower signal", "success"],
    lower_signal: ["Lower signal", "success"],
    review: ["Needs review", "warning"],
    high: ["Higher signal", "danger"],
  };
  const [label, tone] = known[status] || [status || "Unknown", "neutral"];
  return <span className={`status-badge ${tone}`}><span className="status-dot" /> {label}</span>;
}
