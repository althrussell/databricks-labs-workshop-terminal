// App-level event socket (/ws/events) with auto-reconnect: phase changes,
// broadcasts, content updates. Singleton shared by every component.

export type AppEvent =
  | { t: "phase"; phase: string }
  | {
      t: "broadcast";
      message: string;
      level: string;
      ttl_s: number;
      clear_help?: boolean;
      source?: string;
    }
  | {
      t: "help_message";
      message_id: string;
      help_request_id?: string | null;
      sender_role: string;
      sender?: string;
      body: string;
      created_at: string;
      show_banner?: boolean;
    }
  | { t: "help_state"; raised: boolean; note?: string | null }
  | { t: "content_updated" }
  | { t: "obo_refresh" }
  | { t: "reconnected" }
  | { t: "connection_lost"; reason: "reconnect_exhausted" | "authentication_failed" }
  | { t: "pong" };

type Listener = (event: AppEvent) => void;

const listeners = new Set<Listener>();
const AUTH_RELOAD_GUARD = "workshop-events-auth-reloaded";
const MAX_RECONNECT_ATTEMPTS = 12;

function publish(event: AppEvent) {
  listeners.forEach((fn) => fn(event));
}

function parseServerEvent(data: string): AppEvent | null {
  const value = JSON.parse(data) as Record<string, unknown>;
  switch (value.t) {
    case "phase":
      return typeof value.phase === "string" ? value as AppEvent : null;
    case "broadcast":
      return (
        typeof value.message === "string"
        && typeof value.level === "string"
        && typeof value.ttl_s === "number"
      ) ? value as AppEvent : null;
    case "help_message":
      return (
        typeof value.message_id === "string"
        && typeof value.body === "string"
        && typeof value.sender_role === "string"
      ) ? value as AppEvent : null;
    case "help_state":
      return typeof value.raised === "boolean" ? value as AppEvent : null;
    case "content_updated":
    case "obo_refresh":
    case "pong":
      return value as AppEvent;
    default:
      return null;
  }
}

interface SocketLike {
  readyState: number;
  onopen: ((event: any) => void) | null;
  onmessage: ((message: any) => void) | null;
  onclose: ((event: any) => void) | null;
  send(data: string): void;
  close(): void;
}

interface EventConnectionOptions {
  socketFactory: () => SocketLike;
  publish: (event: AppEvent) => void;
  schedule?: (fn: () => void, delay: number) => unknown;
  random?: () => number;
  reload?: () => void;
  storage?: Pick<Storage, "getItem" | "setItem" | "removeItem">;
  maxAttempts?: number;
}

export function createAppEventConnection(options: EventConnectionOptions) {
  const schedule = options.schedule ?? ((fn, delay) => setTimeout(fn, delay));
  const random = options.random ?? Math.random;
  const reload = options.reload ?? (() => location.reload());
  const memoryStorage = new Map<string, string>();
  const storage = options.storage ?? (
    typeof sessionStorage !== "undefined"
      ? sessionStorage
      : {
          getItem: (key: string) => memoryStorage.get(key) ?? null,
          setItem: (key: string, value: string) => {
            memoryStorage.set(key, value);
          },
          removeItem: (key: string) => {
            memoryStorage.delete(key);
          },
        }
  );
  const maxAttempts = options.maxAttempts ?? MAX_RECONNECT_ATTEMPTS;
  let current: SocketLike | null = null;
  let retryDelay = 1000;
  let attempts = 0;
  let hasConnected = false;
  let stopped = false;

  function connect() {
    if (stopped) return;
    const next = options.socketFactory();
    current = next;
    next.onopen = () => {
      if (hasConnected) options.publish({ t: "reconnected" });
      hasConnected = true;
    };
    next.onmessage = (msg) => {
      try {
        const event = parseServerEvent(msg.data);
        if (!event) return;
        // Opening the TCP/WebSocket handshake is not proof of health: a proxy
        // can accept and immediately close forever. Only a valid server frame
        // resets the consecutive-failure ceiling and exponential backoff.
        retryDelay = 1000;
        attempts = 0;
        storage.removeItem(AUTH_RELOAD_GUARD);
        options.publish(event);
      } catch {
        /* ignore malformed frames */
      }
    };
    next.onclose = (ev) => {
      if (current === next) current = null;
      if (stopped) return;
      if (ev.code === 4403) {
        if (storage.getItem(AUTH_RELOAD_GUARD)) {
          options.publish({ t: "connection_lost", reason: "authentication_failed" });
          return;
        }
        storage.setItem(AUTH_RELOAD_GUARD, "1");
        schedule(reload, 1500);
        return;
      }
      attempts += 1;
      if (attempts > maxAttempts) {
        options.publish({ t: "connection_lost", reason: "reconnect_exhausted" });
        return;
      }
      const jitter = random() * retryDelay;
      schedule(connect, retryDelay + jitter);
      retryDelay = Math.min(retryDelay * 2, 15000);
    };
  }

  return {
    start: connect,
    stop: () => {
      stopped = true;
      current?.close();
      current = null;
    },
    ping: () => {
      if (current?.readyState === 1) current.send(JSON.stringify({ t: "ping" }));
    },
  };
}

let appConnection: ReturnType<typeof createAppEventConnection> | null = null;

function connect() {
  if (appConnection) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  appConnection = createAppEventConnection({
    socketFactory: () => new WebSocket(`${proto}://${location.host}/ws/events`),
    publish,
  });
  appConnection.start();
}

// Heartbeat keeps presence fresh and detects half-open connections.
setInterval(() => {
  appConnection?.ping();
}, 20000);

export function onAppEvent(listener: Listener): () => void {
  listeners.add(listener);
  if (!appConnection) connect();
  return () => listeners.delete(listener);
}

interface EventSource {
  addEventListener(type: string, listener: EventListener): void;
  removeEventListener(type: string, listener: EventListener): void;
}

interface VisibilitySource extends EventSource {
  readonly visibilityState: string;
}

interface IdentityRefreshOptions {
  windowTarget?: EventSource;
  documentTarget?: VisibilitySource;
  subscribe?: (listener: Listener) => () => void;
  debounceMs?: number;
}

/** Wire the browser signals that can deliver a fresh Apps-proxy OBO token.
 *
 * The first signal runs immediately. Closely grouped focus, visibility,
 * reconnect, and server nudges are leading-edge coalesced so returning to a tab
 * cannot create a request/reconnect storm.
 */
export function bindIdentityRefresh(
  refresh: () => void | Promise<void>,
  options: IdentityRefreshOptions = {}
): () => void {
  const windowTarget = options.windowTarget ?? window;
  const documentTarget = options.documentTarget ?? document;
  const subscribe = options.subscribe ?? onAppEvent;
  const debounceMs = options.debounceMs ?? 250;
  let running = false;
  let coolingDown = false;
  let pending = false;
  let stopped = false;
  let cooldown: ReturnType<typeof setTimeout> | undefined;

  const flushPending = () => {
    if (!stopped && pending && !running && !coolingDown) {
      pending = false;
      runRefresh();
    }
  };
  const runRefresh = () => {
    running = true;
    coolingDown = true;
    cooldown = setTimeout(() => {
      coolingDown = false;
      flushPending();
    }, debounceMs);
    let result: void | Promise<void>;
    try {
      result = refresh();
    } catch {
      running = false;
      flushPending();
      return;
    }
    Promise.resolve(result)
      .catch(() => undefined)
      .finally(() => {
        running = false;
        flushPending();
      });
  };
  const trigger = () => {
    if (stopped) return;
    if (running || coolingDown) {
      pending = true;
      return;
    }
    runRefresh();
  };
  const onFocus: EventListener = () => trigger();
  const onVisibility: EventListener = () => {
    if (documentTarget.visibilityState === "visible") trigger();
  };
  windowTarget.addEventListener("focus", onFocus);
  documentTarget.addEventListener("visibilitychange", onVisibility);
  const unsubscribe = subscribe((event) => {
    if (event.t === "obo_refresh" || event.t === "reconnected") trigger();
  });

  return () => {
    stopped = true;
    pending = false;
    if (cooldown) clearTimeout(cooldown);
    windowTarget.removeEventListener("focus", onFocus);
    documentTarget.removeEventListener("visibilitychange", onVisibility);
    unsubscribe();
  };
}
