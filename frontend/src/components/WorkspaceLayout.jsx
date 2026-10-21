import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { NavLink, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import Icon from "./Icon";
import OverviewPage from "../pages/OverviewPage";
import HistoryPage from "../pages/HistoryPage";
import LiveCallsPage from "../pages/LiveCallsPage";
import SettingsPage from "../pages/SettingsPage";
import NotFoundPage from "../pages/NotFoundPage";
import { apiErrorMessage, apiService } from "../services/api";
import { mergeScan, normalizeScan, normalizeScanResponse, SENSITIVE_SCAN_FIELDS } from "../utils/scan";

const navItems = [{ to: "/", label: "Overview", icon: "grid" }, { to: "/history", label: "Scan history", icon: "history" }, { to: "/live-calls", label: "Live calls", icon: "radio", planned: true }];
const emptyData = { scans: [], stats: { total_scans: 0, suspicious_results: 0, analyzed_results: 0, inconclusive_results: 0 } };
function mergeUploadedScan(listScan, uploadedEntry) {
  const uploadedFields = Object.fromEntries(
    [...uploadedEntry.fields]
      .filter((field) => !SENSITIVE_SCAN_FIELDS.has(field))
      .map((field) => [field, uploadedEntry.scan[field]]),
  );
  return normalizeScan({ ...listScan, ...uploadedFields });
}

function mergeListResponse(response, uploadedScans) {
  const normalized = normalizeScanResponse(response);
  if (!uploadedScans.size) return normalized;

  const listById = new Map(normalized.scans.map((scan) => [scan.id, scan]));
  const uploadedIds = new Set(uploadedScans.keys());
  const uploaded = [...uploadedScans.entries()].map(([id, entry]) => {
    const listScan = listById.get(id);
    return listScan ? mergeUploadedScan(listScan, entry) : entry.scan;
  });
  const scans = [...uploaded, ...normalized.scans.filter((scan) => !uploadedIds.has(scan.id))];
  return { ...normalized, scans, stats: { ...normalized.stats, total_scans: Math.max(normalized.stats.total_scans || 0, scans.length) } };
}

function uploadNotice(status) {
  if (status === "pending") return "Recording queued for analysis. This result will update automatically.";
  if (status === "processing") return "Recording is processing. This result will update automatically.";
  if (status === "failed") return "Recording analysis failed. Open the result for details.";
  if (status === "completed") return "Recording analyzed. Review the separate voice and scam-language signals.";
  return "Recording validated. Review the result for its current analysis status.";
}

export default function WorkspaceLayout({ user, onSignOut, readOnly = false }) {
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem("deepguard-sidebar") === "collapsed");
  const [theme, setTheme] = useState(() => localStorage.getItem("deepguard-theme") || "light");
  const [mobileOpen, setMobileOpen] = useState(false);
  const [data, setData] = useState(emptyData);
  const [loading, setLoading] = useState(!readOnly);
  const [selectedId, setSelectedId] = useState(null);
  const [currentPreview, setCurrentPreview] = useState(null);
  const [notice, setNotice] = useState("");
  const [loadError, setLoadError] = useState("");
  const listRequestRef = useRef(0);
  const uploadedScansRef = useRef(new Map());
  const currentPreviewRef = useRef(null);
  const location = useLocation();
  const navigate = useNavigate();
  const selectedScan = useMemo(() => data.scans.find((scan) => scan.id === selectedId), [data.scans, selectedId]);
  const pageTitle = location.pathname.includes("history") ? "Scan history" : location.pathname.includes("live-calls") ? "Live calls" : location.pathname.includes("settings") ? "Workspace settings" : "Overview";

  useEffect(() => { document.documentElement.dataset.theme = theme; localStorage.setItem("deepguard-theme", theme); }, [theme]);
  useEffect(() => { localStorage.setItem("deepguard-sidebar", collapsed ? "collapsed" : "expanded"); }, [collapsed]);
  useEffect(() => { if (!readOnly) localStorage.setItem("deepguard-preferred-tab", location.pathname === "/history" ? "history" : "overview"); }, [location.pathname, readOnly]);
  useEffect(() => { setSelectedId(null); }, [location.pathname, readOnly, user?.id]);
  useEffect(() => () => {
    const previewUrl = currentPreviewRef.current?.url;
    if (previewUrl && typeof URL !== "undefined" && typeof URL.revokeObjectURL === "function") URL.revokeObjectURL(previewUrl);
  }, []);
  const loadScans = useCallback((reset = false) => {
    if (readOnly) return null;
    const requestId = ++listRequestRef.current;
    if (reset) setData(emptyData);
    setLoading(true);
    if (reset) setLoadError("");
    apiService.listScans()
      .then(({ data: response }) => {
        if (requestId === listRequestRef.current) setData(mergeListResponse(response, uploadedScansRef.current));
      })
      .catch((error) => {
        if (requestId === listRequestRef.current) setLoadError(apiErrorMessage(error, "Could not load scan history from the server."));
      })
      .finally(() => {
        if (requestId === listRequestRef.current) setLoading(false);
      });
    return requestId;
  }, [readOnly]);

  useEffect(() => {
    if (readOnly) return undefined;
    uploadedScansRef.current.clear();
    const requestId = loadScans(true);
    return () => {
      if (requestId === listRequestRef.current) listRequestRef.current += 1;
    };
  }, [loadScans, readOnly, user?.id]);

  const reload = () => { loadScans(); };
  const deleteScan = async (id) => {
    if (!window.confirm("Delete this scan record? Raw audio was never retained.")) return;
    try { await apiService.deleteScan(id); uploadedScansRef.current.delete(id); setSelectedId(null); setNotice("Scan record deleted."); reload(); }
    catch (error) { setNotice(apiErrorMessage(error, "Could not delete that scan.")); }
  };
  const releasePreview = useCallback((previewUrl) => {
    const current = currentPreviewRef.current;
    if (current?.url && (!previewUrl || current.url === previewUrl)) {
      if (typeof URL !== "undefined" && typeof URL.revokeObjectURL === "function") URL.revokeObjectURL(current.url);
      currentPreviewRef.current = null;
      setCurrentPreview(null);
      return;
    }
    if (previewUrl && typeof URL !== "undefined" && typeof URL.revokeObjectURL === "function") URL.revokeObjectURL(previewUrl);
  }, []);
  useEffect(() => {
    const preview = currentPreviewRef.current;
    if (preview && preview.scanId !== selectedId) releasePreview(preview.url);
  }, [releasePreview, selectedId]);
  const onUploaded = (scan, preview = null) => {
    if (currentPreviewRef.current?.url) releasePreview(currentPreviewRef.current.url);
    const normalized = scan?.id ? normalizeScan(scan) : null;
    if (normalized?.id && preview?.url) {
      const nextPreview = { file: preview.file, scanId: normalized.id, url: preview.url };
      currentPreviewRef.current = nextPreview;
      setCurrentPreview(nextPreview);
    } else if (preview?.url) {
      releasePreview(preview.url);
    }
    if (normalized) {
      const fields = new Set(Object.keys(scan).filter((field) => !SENSITIVE_SCAN_FIELDS.has(field)));
      uploadedScansRef.current = new Map([[normalized.id, { scan: normalized, fields }], ...uploadedScansRef.current]);
      setData((current) => {
        const scans = [normalized, ...current.scans.filter((item) => item.id !== normalized.id)];
        return {
          ...current,
          scans,
          stats: { ...current.stats, total_scans: scans.length },
        };
      });
    }
    setSelectedId(normalized?.id || null);
    setNotice(uploadNotice(scan?.status));
    reload();
  };
  const onScanUpdate = useCallback((scan) => {
    const normalized = scan?.id ? normalizeScan(scan) : null;
    if (!normalized) return;

    const previousEntry = uploadedScansRef.current.get(normalized.id);
    const merged = mergeScan(previousEntry?.scan, normalized);
    const fields = new Set([
      ...(previousEntry?.fields || []),
      ...Object.keys(scan).filter((field) => !SENSITIVE_SCAN_FIELDS.has(field)),
    ]);
    uploadedScansRef.current = new Map([
      [merged.id, { scan: merged, fields }],
      ...[...uploadedScansRef.current.entries()].filter(([id]) => id !== merged.id),
    ]);
    setData((current) => {
      const hasScan = current.scans.some((item) => item.id === merged.id);
      const scans = hasScan
        ? current.scans.map((item) => (item.id === merged.id ? merged : item))
        : [merged, ...current.scans];
      return {
        ...current,
        scans,
        stats: { ...current.stats, total_scans: Math.max(current.stats.total_scans || 0, scans.length) },
      };
    });
  }, []);
  const displayUser = user || { email: "preview@example.invalid" };

  return <div className={`app-shell ${collapsed ? "sidebar-collapsed" : ""} ${mobileOpen ? "mobile-nav-open" : ""}`}>
    <aside className="sidebar">
      <div className="sidebar-top"><NavLink to={readOnly ? "/preview" : "/"} className="brand" onClick={() => setMobileOpen(false)}><span className="brand-mark"><Icon name="shield" size={20} /></span><span className="brand-name">Deep<span>Guard</span></span></NavLink><button className="icon-button collapse-button" onClick={() => setCollapsed(!collapsed)} aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}><Icon name="menu" size={19} /></button></div>
      <div className="workspace-chip"><span className="workspace-avatar">{displayUser.email[0].toUpperCase()}</span><span><strong>{readOnly ? "Read-only preview" : "Personal workspace"}</strong><small>{readOnly ? "No account connected" : displayUser.email}</small></span></div>
      <nav className="main-nav" aria-label="Main navigation"><p className="nav-label">Workspace</p>{navItems.map((item) => <NavLink key={item.to} to={readOnly ? `/preview${item.to === "/" ? "" : item.to}` : item.to} end={item.to === "/"} className={({ isActive }) => `nav-item ${isActive ? "active" : ""}`} onClick={() => setMobileOpen(false)}><Icon name={item.icon} size={18} /><span>{item.label}</span>{item.planned && <span className="planned-dot" title="Unavailable capability">Unavailable</span>}</NavLink>)}<p className="nav-label secondary">Manage</p><NavLink to={readOnly ? "/preview/settings" : "/settings"} className={({ isActive }) => `nav-item ${isActive ? "active" : ""}`} onClick={() => setMobileOpen(false)}><Icon name="settings" size={18} /><span>Settings</span></NavLink></nav>
       <div className="sidebar-bottom"><div className="privacy-note"><Icon name="lock" size={16} /><span><strong>Privacy mode</strong><small>Playback retention is opt-in</small></span></div>{readOnly ? <button className="nav-item sign-out" onClick={() => navigate("/login")}><Icon name="logout" size={18} /><span>Sign in</span></button> : <button className="nav-item sign-out" onClick={onSignOut}><Icon name="logout" size={18} /><span>Sign out</span></button>}</div>
    </aside>
    {mobileOpen && <button className="mobile-overlay" onClick={() => setMobileOpen(false)} aria-label="Close navigation" />}
    <main className="main-content">
      <header className="topbar"><button className="mobile-menu icon-button" onClick={() => setMobileOpen(true)} aria-label="Open navigation"><Icon name="menu" size={20} /></button><div className="breadcrumbs"><span>{readOnly ? "Preview" : "Workspace"}</span><b>/</b><strong>{pageTitle}</strong></div><div className="top-actions"><button className="theme-toggle icon-button" onClick={() => setTheme(theme === "light" ? "dark" : "light")} aria-label={`Switch to ${theme === "light" ? "dark" : "light"} theme`}><Icon name={theme === "light" ? "moon" : "sun"} size={18} /></button><div className="top-user"><span>{displayUser.email[0].toUpperCase()}</span><strong>{readOnly ? "Preview" : displayUser.email.split("@")[0]}</strong></div></div></header>
      {readOnly && <div className="global-notice preview-notice"><Icon name="info" size={16} /><span>Read-only Preview workspace — no account data, private API requests, or actions are enabled.</span><button onClick={() => navigate("/login")} className="link-button">Sign in</button></div>}
      {notice && <div className="global-notice" role="status"><Icon name="info" size={16} /><span>{notice}</span><button onClick={() => setNotice("")} aria-label="Dismiss notification">×</button></div>}
      {loadError && <div className="global-notice error-notice" role="alert"><Icon name="alert" size={16} /><span>{loadError}</span><button onClick={() => { setLoadError(""); reload(); }}>Retry</button></div>}
       <div className="page-content"><Routes><Route index element={<OverviewPage data={data} loading={loading} selectedId={selectedId} selectedScan={selectedScan} currentPreview={currentPreview?.scanId === selectedId ? currentPreview : null} onSelect={setSelectedId} onDelete={deleteScan} onUploaded={onUploaded} onPreviewDiscard={releasePreview} onScanUpdate={onScanUpdate} navigate={navigate} readOnly={readOnly} />} /><Route path="history" element={<HistoryPage data={data} loading={loading} selectedId={selectedId} selectedScan={selectedScan} currentPreview={currentPreview?.scanId === selectedId ? currentPreview : null} onSelect={setSelectedId} onDelete={deleteScan} onScanUpdate={onScanUpdate} navigate={navigate} readOnly={readOnly} />} /><Route path="live-calls" element={<LiveCallsPage />} /><Route path="settings" element={<SettingsPage theme={theme} setTheme={setTheme} />} /><Route path="*" element={<NotFoundPage />} /></Routes></div>
    </main>
  </div>;
}
