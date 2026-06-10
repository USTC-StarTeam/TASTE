import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles.css";

type ErrorBoundaryState = { error: Error | null };

class ErrorBoundary extends React.Component<React.PropsWithChildren, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error("UI render error", error, info);
  }

  render() {
    if (this.state.error) {
      return (
        <main className="appCrash">
          <section className="panel crashPanel">
            <h1>UI render error</h1>
            <p>The page hit a render-time data error instead of going blank. Refresh after the backend state updates; the API and supervision can continue running.</p>
            <pre>{this.state.error.message}</pre>
            <button onClick={() => this.setState({ error: null })}>Try rendering again</button>
          </section>
        </main>
      );
    }
    return this.props.children;
  }
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>,
);
