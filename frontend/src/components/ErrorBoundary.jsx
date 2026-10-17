import { Component } from "react";

export default class ErrorBoundary extends Component {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    if (!this.state.failed) return this.props.children;
    return <main className="app-loading" role="alert"><h1>Workspace temporarily unavailable</h1><p>Refresh this page to try again.</p><button className="button primary" onClick={() => window.location.reload()}>Refresh workspace</button></main>;
  }
}
