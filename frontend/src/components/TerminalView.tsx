import { useEffect, useImperativeHandle, useRef, type Ref } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import "@xterm/xterm/css/xterm.css";
import { terminalGatewayLimit } from "../gatewayLimit";
import { terminalExitNotice } from "../terminalExit";
import {
  browserClipboardEnv,
  copyNotice,
  copyText,
  IS_MAC,
  pasteIsImageOnly,
  pasteNotice,
  type ClipboardNotice,
} from "../terminalClipboard";
import { decideTerminalKey } from "../terminalKeys";
import { keySequence, type TouchKey } from "../touchKeys";
import type { AttendeeErrorCode } from "../telemetry";
import { notifyWorkshopClosing, workshopNotice } from "../workshopLifecycle";
import { TOUCH_QUERY } from "./useMediaQuery";

/** What the toolbar's Copy and Paste buttons can ask of the live terminal. */
export interface TerminalHandle {
  /** Copy the selection; the outcome arrives through `onClipboard`. */
  copySelection: () => void;
  /** Type text as a paste. False when the terminal can't take input. */
  paste: (text: string) => boolean;
  /** Return the keyboard to the prompt once a button or dialog is done. */
  focus: () => void;
  /** Type `text` as a paste, then press Enter: the composer's Send. */
  submit: (text: string) => boolean;
  /** Press one key a phone keyboard lacks (Esc, Tab, an arrow, Ctrl+C). */
  press: (key: TouchKey) => boolean;
}

/** Gap between a pasted prompt and its Enter. Agent TUIs that time keystrokes
 * to spot a paste would otherwise read the Enter as a newline in the prompt. */
const SUBMIT_ENTER_DELAY_MS = 120;

/** Phones get a smaller font so an agent TUI has room for ~45 columns. */
const NARROW = "(max-width: 700px)";
const fontSizeFor = (narrow: boolean) => (narrow ? 12 : 13);

interface Props {
  ref?: Ref<TerminalHandle>;
  sessionId: string;
  active: boolean;
  readOnly?: boolean;
  onExit: (sessionId: string) => void;
  onGatewayLimit?: (
    code: Extract<AttendeeErrorCode, "gateway_allowance_exhausted" | "gateway_rate_limited">
  ) => void;
  /** Every copy and paste outcome, so none of them is silent. */
  onClipboard?: (notice: ClipboardNotice) => void;
}

// One xterm + one websocket per session, kept alive across tab switches
// (the component stays mounted; `active` only toggles visibility). The
// server holds the PTY and scrollback, so a dropped socket reconnects and
// replays seamlessly.
export default function TerminalView({ ref, sessionId, active, readOnly = false, onExit, onGatewayLimit, onClipboard }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const closedRef = useRef(false);
  const activeRef = useRef(active);
  activeRef.current = active;
  const readOnlyRef = useRef(readOnly);
  readOnlyRef.current = readOnly;
  const onClipboardRef = useRef(onClipboard);
  onClipboardRef.current = onClipboard;
  const sendResizeRef = useRef<() => void>(() => {});
  const submitTimersRef = useRef<Set<number>>(new Set());

  function inputOpen() {
    return !readOnlyRef.current && !closedRef.current && socketRef.current?.readyState === WebSocket.OPEN;
  }

  function copySelection(term: Terminal) {
    const text = term.hasSelection() ? term.getSelection() : "";
    void copyText(text, browserClipboardEnv()).then((outcome) => {
      // Clear the selection only once the copy has landed. If every clipboard
      // path was refused the selection stays, so the attendee can still copy
      // it from the context menu — and the agent never sees an interrupt.
      if (outcome === "copied") term.clearSelection();
      onClipboardRef.current?.(copyNotice(outcome, IS_MAC));
    });
  }

  useImperativeHandle(ref, () => ({
    copySelection: () => {
      if (termRef.current) copySelection(termRef.current);
    },
    paste: (text: string) => {
      const term = termRef.current;
      if (!term || !inputOpen()) return false;
      // xterm's paste normalises newlines to CR and wraps the text in
      // bracketed-paste markers when the agent asked for them, so a multiline
      // prompt arrives as one block. No Enter is added: sending is the
      // attendee's call.
      term.paste(text);
      return true;
    },
    focus: () => termRef.current?.focus(),
    submit: (text: string) => {
      const term = termRef.current;
      if (!term || !inputOpen() || !text.trim()) return false;
      // Sending is the attendee's call: this runs only from their Send tap.
      // A trailing newline would be a second Enter inside the paste.
      term.paste(text.replace(/\s+$/, ""));
      const timer = window.setTimeout(() => {
        submitTimersRef.current.delete(timer);
        if (inputOpen()) term.input("\r", true);
      }, SUBMIT_ENTER_DELAY_MS);
      submitTimersRef.current.add(timer);
      return true;
    },
    press: (key: TouchKey) => {
      const term = termRef.current;
      if (!term || !inputOpen()) return false;
      // As typed: scrolls to the prompt and clears a selection, without moving
      // focus, so a key-bar tap doesn't pop the phone keyboard open.
      term.input(keySequence(key, term.modes.applicationCursorKeysMode), true);
      return true;
    },
  }), []);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const narrow = window.matchMedia(NARROW);
    const term = new Terminal({
      cursorBlink: true,
      fontSize: fontSizeFor(narrow.matches),
      fontFamily: "'JetBrains Mono', 'SF Mono', Menlo, monospace",
      theme: {
        background: "#0e1116",
        foreground: "#d8dee9",
        cursor: "#ff3621",
        selectionBackground: "#2e3440",
      },
      scrollback: 5000,
      disableStdin: readOnlyRef.current,
      // Agents that capture the mouse disable drag-to-select. Shift+drag
      // forces a selection elsewhere; on macOS xterm needs this for ⌥+drag.
      macOptionClickForcesSelection: true,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.loadAddon(new WebLinksAddon());
    // Returning false stops xterm from turning a chord into a control byte.
    // "browser" then leaves the browser's default in place — its native paste
    // event, which xterm handles itself and which needs no permission.
    term.attachCustomKeyEventHandler((event) => {
      const decision = decideTerminalKey(event, term.hasSelection(), IS_MAC);
      if (decision === "copy") {
        // Ctrl+Shift+C would otherwise open the browser's element inspector.
        event.preventDefault();
        copySelection(term);
        return false;
      }
      return decision === "terminal";
    });
    // Native copies (Cmd+C, the right-click menu) are written by xterm's own
    // handler, which cancels the event once it has set the selection. Confirm
    // them like the Ctrl+C path so every copy looks the same.
    const onNativeCopy = (event: ClipboardEvent) => {
      if (event.defaultPrevented && term.hasSelection()) onClipboardRef.current?.(copyNotice("copied", IS_MAC));
    };
    // Captured before xterm's textarea listener, which stops propagation. A
    // screenshot pasted with Ctrl+V types nothing, so say where it should go.
    const onNativePaste = (event: ClipboardEvent) => {
      if (pasteIsImageOnly(event.clipboardData)) onClipboardRef.current?.(pasteNotice("image"));
    };
    container.addEventListener("copy", onNativeCopy);
    container.addEventListener("paste", onNativePaste, true);

    // xterm 6's SmoothScrollableElement listens only to wheel events, so finger
    // drags on touch screens must drive scrollLines directly.
    let touchY: number | null = null;
    let touchRemainder = 0;
    const onTouchStart = (event: TouchEvent) => {
      if (event.touches.length === 1) {
        touchY = event.touches[0].clientY;
        touchRemainder = 0;
      } else {
        touchY = null;
      }
    };
    const onTouchMove = (event: TouchEvent) => {
      if (touchY === null || event.touches.length !== 1) return;
      const nextY = event.touches[0].clientY;
      const dy = touchY - nextY;
      touchY = nextY;
      const cellHeight = container.clientHeight / Math.max(1, term.rows) || 16;
      const total = touchRemainder + dy / cellHeight;
      const lines = total > 0 ? Math.floor(total) : Math.ceil(total);
      touchRemainder = total - lines;
      if (lines !== 0 && term.buffer.active.baseY > 0) {
        term.scrollLines(lines);
        if (event.cancelable) event.preventDefault();
      }
    };
    const onTouchEnd = () => {
      touchY = null;
      touchRemainder = 0;
    };
    container.addEventListener("touchstart", onTouchStart, { passive: true });
    container.addEventListener("touchmove", onTouchMove, { passive: false });
    container.addEventListener("touchend", onTouchEnd, { passive: true });
    container.addEventListener("touchcancel", onTouchEnd, { passive: true });

    term.open(container);
    fit.fit();
    termRef.current = term;
    fitRef.current = fit;

    let retryDelay = 500;
    let attempts = 0;
    let disposed = false;
    let limitWindow = "";
    let reportedLimit: AttendeeErrorCode | null = null;
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
          // Keep only enough local output to bridge websocket chunk boundaries.
          // Raw terminal output never leaves this browser tab.
          limitWindow = (limitWindow + String(data.data)).slice(-4096);
          const limit = terminalGatewayLimit(limitWindow);
          if (limit && limit !== reportedLimit) {
            reportedLimit = limit;
            onGatewayLimit?.(limit);
          }
        } else if (data.t === "exit") {
          if (closedRef.current) return;
          closedRef.current = true;
          if (data.reason === "workshop_closing") notifyWorkshopClosing();
          term.write(`\r\n\x1b[33m[${terminalExitNotice(data)}]\x1b[0m\r\n`);
          socket.close();
          onExit(sessionId);
        }
      };
      socket.onclose = (ev) => {
        socketRef.current = null;
        if (disposed || closedRef.current) return;
        if (ev.code === 4410) {
          closedRef.current = true;
          term.options.disableStdin = true;
          term.write(`\r\n\x1b[33m[${workshopNotice("closing")}]\x1b[0m\r\n`);
          notifyWorkshopClosing();
          return;
        }
        if (ev.code === 4404) {
          // Session is gone server-side (reaped or restart) — don't retry. The
          // server now accepts before closing, so this code actually arrives.
          closedRef.current = true;
          term.write("\r\n\x1b[33m[session no longer exists — the workshop may have restarted. Select Relaunch to continue.]\x1b[0m\r\n");
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
      if (
        activeRef.current &&
        !readOnlyRef.current &&
        !closedRef.current &&
        socket?.readyState === WebSocket.OPEN &&
        term.cols > 0 &&
        term.rows > 0
      ) {
        socket.send(JSON.stringify({ t: "resize", cols: term.cols, rows: term.rows }));
      }
    }
    sendResizeRef.current = sendResize;

    const inputDisposable = term.onData((data) => {
      if (inputOpen()) socketRef.current?.send(JSON.stringify({ t: "input", data }));
    });

    const resizeObserver = new ResizeObserver(() => {
      if (!activeRef.current) return;
      fit.fit();
      sendResize();
    });
    resizeObserver.observe(container);

    const onNarrowChange = () => {
      term.options.fontSize = fontSizeFor(narrow.matches);
      if (!activeRef.current) return;
      fit.fit();
      sendResize();
    };
    narrow.addEventListener("change", onNarrowChange);

    // A phone and a laptop can share this session, and the PTY has one size.
    // The device the attendee comes back to (or clicks into) claims it before
    // they type; the server also re-applies the typing socket's size (server/ws.py).
    const reclaimSize = () => {
      if (document.visibilityState === "visible") sendResize();
    };
    document.addEventListener("visibilitychange", reclaimSize);
    window.addEventListener("focus", reclaimSize);
    container.addEventListener("pointerdown", reclaimSize);

    const heartbeat = setInterval(() => {
      const socket = socketRef.current;
      if (!readOnlyRef.current && !closedRef.current && socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ t: "ping" }));
      }
    }, 20000);

    connect();

    return () => {
      disposed = true;
      sendResizeRef.current = () => {};
      for (const timer of submitTimersRef.current) window.clearTimeout(timer);
      submitTimersRef.current.clear();
      clearInterval(heartbeat);
      resizeObserver.disconnect();
      narrow.removeEventListener("change", onNarrowChange);
      document.removeEventListener("visibilitychange", reclaimSize);
      window.removeEventListener("focus", reclaimSize);
      container.removeEventListener("pointerdown", reclaimSize);
      container.removeEventListener("copy", onNativeCopy);
      container.removeEventListener("paste", onNativePaste, true);
      container.removeEventListener("touchstart", onTouchStart);
      container.removeEventListener("touchmove", onTouchMove);
      container.removeEventListener("touchend", onTouchEnd);
      container.removeEventListener("touchcancel", onTouchEnd);
      inputDisposable.dispose();
      socketRef.current?.close();
      term.dispose();
    };
  }, [sessionId, onExit, onGatewayLimit]);

  useEffect(() => {
    if (termRef.current) termRef.current.options.disableStdin = readOnly;
  }, [readOnly]);

  useEffect(() => {
    if (active) {
      fitRef.current?.fit();
      sendResizeRef.current();
      // On touch screens, focusing xterm opens the raw OS keyboard over the terminal;
      // the key bar's explicit keyboard button still focuses it on demand.
      if (!window.matchMedia(TOUCH_QUERY).matches) {
        termRef.current?.focus();
      }
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
