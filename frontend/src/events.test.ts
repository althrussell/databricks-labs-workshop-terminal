import assert from "node:assert/strict";
import { test } from "node:test";
import type { AppEvent } from "./events.ts";

type Listener = (event: AppEvent) => void;

const nativeSetInterval = globalThis.setInterval;
globalThis.setInterval = ((...args: Parameters<typeof setInterval>) => {
  const timer = nativeSetInterval(...args);
  timer.unref();
  return timer;
}) as typeof setInterval;

async function bindForTest(refresh: () => void | Promise<void>, debounceMs = 20) {
  const events = await import("./events.ts");
  assert.equal(
    typeof events.bindIdentityRefresh,
    "function",
    "bindIdentityRefresh must wire browser recovery triggers"
  );

  const windowTarget = new EventTarget();
  const documentTarget = new EventTarget();
  Object.defineProperty(documentTarget, "visibilityState", {
    value: "hidden",
    configurable: true,
  });
  let appListener: Listener | undefined;
  const cleanup = events.bindIdentityRefresh(refresh, {
    windowTarget,
    documentTarget,
    debounceMs,
    subscribe: (listener: Listener) => {
      appListener = listener;
      return () => {
        appListener = undefined;
      };
    },
  });
  return { windowTarget, documentTarget, cleanup, emit: (event: AppEvent) => appListener?.(event) };
}

test("focus refreshes identity immediately", async () => {
  let calls = 0;
  const bound = await bindForTest(() => {
    calls += 1;
  });

  bound.windowTarget.dispatchEvent(new Event("focus"));

  assert.equal(calls, 1);
  bound.cleanup();
});

test("only becoming visible refreshes identity", async () => {
  let calls = 0;
  const bound = await bindForTest(() => {
    calls += 1;
  });

  bound.documentTarget.dispatchEvent(new Event("visibilitychange"));
  assert.equal(calls, 0);
  Object.defineProperty(bound.documentTarget, "visibilityState", {
    value: "visible",
    configurable: true,
  });
  bound.documentTarget.dispatchEvent(new Event("visibilitychange"));

  assert.equal(calls, 1);
  bound.cleanup();
});

test("obo refresh and reconnect triggers are coalesced", async () => {
  let calls = 0;
  const bound = await bindForTest(() => {
    calls += 1;
  }, 30);

  bound.emit({ t: "obo_refresh" });
  bound.emit({ t: "reconnected" });
  bound.windowTarget.dispatchEvent(new Event("focus"));

  assert.equal(calls, 1);
  await new Promise((resolve) => setTimeout(resolve, 40));
  bound.emit({ t: "reconnected" });
  assert.equal(calls, 2);
  bound.cleanup();
});

test("obo refresh during an in-flight refresh runs once afterward", async () => {
  let calls = 0;
  let releaseFirst: (() => void) | undefined;
  const firstRefresh = new Promise<void>((resolve) => {
    releaseFirst = resolve;
  });
  const bound = await bindForTest(() => {
    calls += 1;
    return calls === 1 ? firstRefresh : Promise.resolve();
  }, 20);

  bound.windowTarget.dispatchEvent(new Event("focus"));
  bound.emit({ t: "obo_refresh" });
  bound.emit({ t: "reconnected" });
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.equal(calls, 1);

  releaseFirst?.();
  await new Promise((resolve) => setTimeout(resolve, 0));

  assert.equal(calls, 2);
  bound.cleanup();
});

class FakeSocket {
  static OPEN = 1;
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((message: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  send() {}
  close() {}
}

test("event reconnect stops after the finite attempt ceiling and signals loss", async () => {
  const events = await import("./events.ts");
  const sockets: FakeSocket[] = [];
  const scheduled: Array<() => void> = [];
  const published: AppEvent[] = [];
  const connection = events.createAppEventConnection({
    socketFactory: () => {
      const socket = new FakeSocket();
      sockets.push(socket);
      return socket;
    },
    schedule: (fn) => {
      scheduled.push(fn);
      return 0;
    },
    random: () => 0,
    publish: (event) => published.push(event),
    maxAttempts: 3,
  });

  connection.start();
  for (let attempt = 0; attempt < 4; attempt += 1) {
    sockets.at(-1)?.onclose?.({ code: 1006 });
    scheduled.shift()?.();
  }

  assert.equal(sockets.length, 4);
  assert.deepEqual(published.at(-1), {
    t: "connection_lost",
    reason: "reconnect_exhausted",
  });
  assert.equal(scheduled.length, 0);
  connection.stop();
});

test("accept-then-immediate-close flapping still exhausts reconnect attempts", async () => {
  const events = await import("./events.ts");
  const sockets: FakeSocket[] = [];
  const scheduled: Array<() => void> = [];
  const published: AppEvent[] = [];
  const connection = events.createAppEventConnection({
    socketFactory: () => {
      const socket = new FakeSocket();
      sockets.push(socket);
      return socket;
    },
    schedule: (fn) => {
      scheduled.push(fn);
      return 0;
    },
    random: () => 0,
    publish: (event) => published.push(event),
    maxAttempts: 3,
  });

  connection.start();
  for (let attempt = 0; attempt < 4; attempt += 1) {
    sockets.at(-1)?.onopen?.();
    sockets.at(-1)?.onclose?.({ code: 1006 });
    scheduled.shift()?.();
  }

  assert.equal(sockets.length, 4);
  assert.deepEqual(published.at(-1), {
    t: "connection_lost",
    reason: "reconnect_exhausted",
  });
  assert.equal(scheduled.length, 0);
  connection.stop();
});

test("a valid server message resets reconnect attempts and backoff", async () => {
  const events = await import("./events.ts");
  const sockets: FakeSocket[] = [];
  const scheduled: Array<{ fn: () => void; delay: number }> = [];
  const published: AppEvent[] = [];
  const connection = events.createAppEventConnection({
    socketFactory: () => {
      const socket = new FakeSocket();
      sockets.push(socket);
      return socket;
    },
    schedule: (fn, delay) => {
      scheduled.push({ fn, delay });
      return 0;
    },
    random: () => 0,
    publish: (event) => published.push(event),
    maxAttempts: 2,
  });

  connection.start();
  sockets[0].onopen?.();
  sockets[0].onclose?.({ code: 1006 });
  assert.equal(scheduled[0].delay, 1000);
  scheduled.shift()?.fn();

  sockets[1].onopen?.();
  sockets[1].onclose?.({ code: 1006 });
  assert.equal(scheduled[0].delay, 2000);
  scheduled.shift()?.fn();

  sockets[2].onopen?.();
  sockets[2].onmessage?.({ data: '{"t":"pong"}' });
  sockets[2].onclose?.({ code: 1006 });
  assert.equal(scheduled[0].delay, 1000);
  assert.deepEqual(published.at(-1), { t: "pong" });
  connection.stop();
});

test("event auth failure reloads at most once per tab", async () => {
  const events = await import("./events.ts");
  const sockets: FakeSocket[] = [];
  const scheduled: Array<() => void> = [];
  const published: AppEvent[] = [];
  const values = new Map<string, string>();
  let reloads = 0;
  const options = {
    socketFactory: () => {
      const socket = new FakeSocket();
      sockets.push(socket);
      return socket;
    },
    schedule: (fn: () => void) => {
      scheduled.push(fn);
      return 0;
    },
    publish: (event: AppEvent) => published.push(event),
    reload: () => {
      reloads += 1;
    },
    storage: {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
      removeItem: (key: string) => values.delete(key),
    },
  };

  const first = events.createAppEventConnection(options);
  first.start();
  sockets.at(-1)?.onclose?.({ code: 4403 });
  scheduled.shift()?.();
  assert.equal(reloads, 1);
  first.stop();

  const second = events.createAppEventConnection(options);
  second.start();
  sockets.at(-1)?.onclose?.({ code: 4403 });
  assert.equal(reloads, 1);
  assert.deepEqual(published.at(-1), {
    t: "connection_lost",
    reason: "authentication_failed",
  });
  second.stop();
});

test("valid server frame clears auth reload guard for a later auth failure", async () => {
  const events = await import("./events.ts");
  const sockets: FakeSocket[] = [];
  const scheduled: Array<() => void> = [];
  const values = new Map<string, string>();
  let reloads = 0;
  const connection = events.createAppEventConnection({
    socketFactory: () => {
      const socket = new FakeSocket();
      sockets.push(socket);
      return socket;
    },
    schedule: (fn) => {
      scheduled.push(fn);
      return 0;
    },
    publish: () => undefined,
    reload: () => {
      reloads += 1;
    },
    storage: {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
      removeItem: (key: string) => values.delete(key),
    },
  });

  connection.start();
  sockets[0].onclose?.({ code: 4403 });
  scheduled.shift()?.();
  assert.equal(reloads, 1);

  connection.start();
  sockets[1].onmessage?.({ data: "malformed" });
  sockets[1].onclose?.({ code: 4403 });
  assert.equal(scheduled.length, 0);

  connection.start();
  sockets[2].onmessage?.({ data: '{"t":"pong"}' });
  sockets[2].onclose?.({ code: 4403 });
  scheduled.shift()?.();
  assert.equal(reloads, 2);
  connection.stop();
});
