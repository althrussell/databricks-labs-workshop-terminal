import { useEffect, useRef } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import "@xterm/xterm/css/xterm.css";

interface Props {
  sessionId: string;
  active: boolean;
  onExit: (sessionId: string) => void;
}

// One xterm + one websocket per session, kept alive across tab switches
// (the component stays mounted; `active` only toggles visibility). The
// server holds the PTY and scrollback, so a dropped socket reconnects and
// replays seamlessly.
export default function TerminalView({ sessionId, active, onExit }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const closedRef = useRef(false);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const term = new Terminal({
      cursorBlink: true,
      fontSize: 13,
      fontFamily: "'JetBrains Mono', 'SF Mono', Menlo, monospace",
      theme: {
        background: "#0e1116",
        foreground: "#d8dee9",
        cursor: "#ff3621",
        selectionBackground: "#2e3440",
      },
      scrollback: 5000,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.loadAddon(new WebLinksAddon());
    term.open(container);
    fit.fit();
    termRef.current = term;
    fitRef.current = fit;

    let retryDelay = 500;
    let attempts = 0;
    let disposed = false;
    // Ceiling on consecutive failed reconnects so N tabs hitting a down/empty
    // instance can't turn into an unbounded request storm against it.
    const MAX_ATTEMPTS = 12;

    function connect() {
      if (disposed || closedRef.current) return;
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const socket = new WebSocket(`${proto}://${location.host}/ws/sessions/${sessionId}`);
      socketRef.current = socket;

      socket.onopen = () => {
        retryDelay = 500;
        attempts = 0;
        term.clear();
        sendResize();
      };
      socket.onmessage = (msg) => {
        const data = JSON.parse(msg.data);
        if (data.t === "replay" || data.t === "output") {
          term.write(data.data);
        } else if (data.t === "exit") {
          closedRef.current = true;
          term.write("\r\n\x1b[90m[session ended]\x1b[0m\r\n");
          onExit(sessionId);
        }
      };
      socket.onclose = (ev) => {
        socketRef.current = null;
        if (disposed || closedRef.current) return;
        if (ev.code === 4404) {
          // Session is gone server-side (reaped or restart) — don't retry. The
          // server now accepts before closing, so this code actually arrives.
          closedRef.current = true;
          term.write("\r\n\x1b[90m[session no longer exists — the workshop may have restarted. Relaunch it from Home.]\x1b[0m\r\n");
          onExit(sessionId);
          return;
        }
        if (ev.code === 4403) {
          // Auth failed (e.g. the forwarded identity/token went stale). Blind
          // retries can't fix this — reload once to refresh the proxy identity.
          closedRef.current = true;
          term.write("\r\n\x1b[33m[session expired — refreshing…]\x1b[0m\r\n");
          setTimeout(() => location.reload(), 1500);
          return;
        }
        attempts += 1;
        if (attempts > MAX_ATTEMPTS) {
          closedRef.current = true;
          term.write("\r\n\x1b[31m[lost connection to the workshop — reload the page to reconnect]\x1b[0m\r\n");
          return;
        }
        term.write("\r\n\x1b[33m[reconnecting…]\x1b[0m\r\n");
        // Exponential backoff with jitter to avoid a synchronized thundering herd.
        const jitter = Math.random() * retryDelay;
        setTimeout(connect, retryDelay + jitter);
        retryDelay = Math.min(retryDelay * 2, 10000);
      };
    }

    function sendResize() {
      const socket = socketRef.current;
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ t: "resize", cols: term.cols, rows: term.rows }));
      }
    }

    const inputDisposable = term.onData((data) => {
      const socket = socketRef.current;
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ t: "input", data }));
      }
    });

    const resizeObserver = new ResizeObserver(() => {
      fit.fit();
      sendResize();
    });
    resizeObserver.observe(container);

    const heartbeat = setInterval(() => {
      const socket = socketRef.current;
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ t: "ping" }));
      }
    }, 20000);

    connect();

    return () => {
      disposed = true;
      clearInterval(heartbeat);
      resizeObserver.disconnect();
      inputDisposable.dispose();
      socketRef.current?.close();
      term.dispose();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  useEffect(() => {
    if (active) {
      fitRef.current?.fit();
      termRef.current?.focus();
    }
  }, [active]);

  return (
    <div
      ref={containerRef}
      className="terminal-container"
      style={{ display: active ? "block" : "none" }}
    />
  );
}
