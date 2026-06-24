// App-level event socket (/ws/events) with auto-reconnect: phase changes,
// broadcasts, content updates. Singleton shared by every component.

export type AppEvent =
  | { t: "phase"; phase: string }
  | { t: "broadcast"; message: string; level: string; ttl_s: number }
  | { t: "content_updated" }
  | { t: "pong" };

type Listener = (event: AppEvent) => void;

const listeners = new Set<Listener>();
let socket: WebSocket | null = null;
let retryDelay = 1000;

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${proto}://${location.host}/ws/events`);

  socket.onopen = () => {
    retryDelay = 1000;
  };
  socket.onmessage = (msg) => {
    try {
      const event = JSON.parse(msg.data) as AppEvent;
      listeners.forEach((fn) => fn(event));
    } catch {
      /* ignore malformed frames */
    }
  };
  socket.onclose = (ev) => {
    socket = null;
    if (ev.code === 4403) {
      // Auth failed (stale forwarded identity) — reload once to refresh it
      // rather than reconnecting blindly forever.
      setTimeout(() => location.reload(), 1500);
      return;
    }
    // Exponential backoff with jitter so all tabs don't reconnect in lockstep.
    const jitter = Math.random() * retryDelay;
    setTimeout(connect, retryDelay + jitter);
    retryDelay = Math.min(retryDelay * 2, 15000);
  };
}

// Heartbeat keeps presence fresh and detects half-open connections.
setInterval(() => {
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ t: "ping" }));
  }
}, 20000);

export function onAppEvent(listener: Listener): () => void {
  listeners.add(listener);
  if (!socket) connect();
  return () => listeners.delete(listener);
}
