import Icon from "./Icon";
import StatusBadge from "./StatusBadge";
import { formatBytes, formatDate, formatDuration } from "../utils/format";

export default function HistoryTable({ scans, loading, onSelect, onDelete }) {
  if (loading) return <div className="table-card loading-table" role="status"><div className="loader" /><span>Loading your history…</span></div>;
  if (!scans.length) return null;
  return <div className="table-card"><table className="scan-table"><caption className="sr-only">Scan history</caption><thead><tr><th scope="col">Recording</th><th scope="col">Result status</th><th scope="col">Model</th><th scope="col">Analyzed</th><th scope="col"><span className="sr-only">Actions</span></th></tr></thead><tbody>{scans.map((scan) => <tr className="table-row" key={scan.id}><td className="file-cell"><span className="file-icon"><Icon name="waveform" size={16} /></span><span><strong>{scan.filename}</strong><small>{formatBytes(scan.size_bytes)} · {formatDuration(scan.duration_seconds)}</small></span></td><td><StatusBadge status={scan.status} /></td><td className="muted-cell">{scan.model_version || "not-configured"}</td><td className="muted-cell">{formatDate(scan.created_at)}</td><td className="row-actions"><button className="row-view" onClick={() => onSelect(scan.id)}>View <Icon name="arrow" size={14} /></button><button className="delete-action" onClick={() => onDelete(scan.id)} aria-label={`Delete ${scan.filename}`} title="Delete scan"><Icon name="trash" size={15} /></button></td></tr>)}</tbody></table></div>;
}
