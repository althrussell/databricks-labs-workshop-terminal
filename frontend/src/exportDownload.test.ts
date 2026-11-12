import assert from "node:assert/strict";
import test from "node:test";
import { api, ApiError } from "./api.ts";
import { onWorkshopClosing } from "./workshopLifecycle.ts";
import {
  ExportInterruptedError,
  REVOKE_DELAY_MS,
  SIGN_IN_ENDED_MESSAGE,
  TRANSFER_INTERRUPTED_MESSAGE,
  exportBodyFailure,
  exportTransportFailure,
  isSignInRedirect,
  saveDownload,
  type DownloadEnv,
} from "./exportDownload.ts";

function response(init: {
  status?: number;
  type?: string;
  redirected?: boolean;
  headers?: Record<string, string>;
  body?: Blob;
  json?: unknown;
  blob?: () => Promise<Blob>;
}): Response {
  const { status = 200, type = "basic", redirected = false, headers = {}, body = new Blob([]), json = {}, blob } = init;
  return {
    status,
    type,
    redirected,
    ok: status >= 200 && status < 300,
    statusText: "",
    headers: new Headers(headers),
    blob: blob ?? (async () => body),
    json: async () => json,
  } as unknown as Response;
}

/** Run api.exportCode with a stubbed network, restoring fetch afterwards. */
async function withFetch<T>(stub: (url: string, init?: RequestInit) => Promise<Response>, run: () => Promise<T>): Promise<T> {
  const original = globalThis.fetch;
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => stub(String(input), init)) as typeof fetch;
  try {
    return await run();
  } finally {
    globalThis.fetch = original;
  }
}

test("export POSTs /api/export with manual redirects so a sign-in hop is seen, not followed", async () => {
  const seen: Array<{ url: string; init?: RequestInit }> = [];
  const zip = new Blob(["zip-bytes"], { type: "application/zip" });
  const result = await withFetch(async (url, init) => {
    seen.push({ url, init });
    return response({
      headers: { "Content-Disposition": 'attachment; filename="workshop-code-1.zip"', "X-Workshop-Export-Path": "/Volumes/c/s/workshop_exports/code/x.zip" },
      body: zip,
    });
  }, () => api.exportCode());
  assert.equal(seen.length, 1);
  assert.equal(seen[0].url, "/api/export");
  assert.equal(seen[0].init?.method, "POST");
  assert.equal(seen[0].init?.redirect, "manual");
  assert.equal(result.filename, "workshop-code-1.zip");
  assert.equal(result.volumePath, "/Volumes/c/s/workshop_exports/code/x.zip");
  assert.equal(result.blob.size, zip.size);
});

test("an opaque redirect from an expired proxy session asks the attendee to reload", async () => {
  await assert.rejects(
    withFetch(async () => response({ status: 0, type: "opaqueredirect" }), () => api.exportCode()),
    (error: unknown) => {
      assert.ok(error instanceof ExportInterruptedError);
      assert.equal(error.kind, "sign_in");
      assert.equal(error.message, SIGN_IN_ENDED_MESSAGE);
      assert.match(error.message, /Reload this tab/);
      return true;
    },
  );
});

test("a 3xx that did get through is treated as the same sign-in interruption", async () => {
  await assert.rejects(withFetch(async () => response({ status: 302 }), () => api.exportCode()), ExportInterruptedError);
  await assert.rejects(withFetch(async () => response({ status: 200, redirected: true }), () => api.exportCode()), ExportInterruptedError);
});

test("the browser's bare 'Failed to fetch' from a refused cross-origin hop is named, not shown raw", async () => {
  await assert.rejects(
    withFetch(async () => { throw new TypeError("Failed to fetch"); }, () => api.exportCode()),
    (error: unknown) => error instanceof ExportInterruptedError && error.message === SIGN_IN_ENDED_MESSAGE,
  );
});

test("a server error keeps its own message for the caller", async () => {
  await assert.rejects(
    withFetch(async () => response({ status: 503, headers: {} }), () => api.exportCode()),
    (error: unknown) => error instanceof ApiError && error.status === 503,
  );
});

test("export passes an optional project filter as a query parameter", async () => {
  const urls: string[] = [];
  await withFetch(async (url) => {
    urls.push(url);
    return response({ headers: { "Content-Disposition": 'attachment; filename="workshop-code-1.zip"' } });
  }, () => api.exportCode("garden build"));
  assert.deepEqual(urls, ["/api/export?project=garden%20build"]);
});

test("a 409 workshop_closing refusal on export notifies lifecycle listeners immediately", async () => {
  let closedCount = 0;
  const off = onWorkshopClosing(() => { closedCount += 1; });
  try {
    await assert.rejects(
      withFetch(
        async () => response({
          status: 409,
          json: { detail: { code: "workshop_closing", message: "This workshop is closing. New input is disabled." } },
        }),
        () => api.exportCode(),
      ),
      (error: unknown) => error instanceof ApiError && error.status === 409,
    );
    assert.equal(closedCount, 1);
  } finally {
    off();
  }
});

test("a 403 wrong_seat_identity refusal on export latches wrong-identity state and blocks retries", async () => {
  const fresh = await import("./api.ts?wrong-seat-export");
  const details: string[] = [];
  const off = fresh.onWrongIdentity((detail: string) => details.push(detail));
  let networkCalls = 0;
  try {
    await assert.rejects(
      withFetch(
        async () => {
          networkCalls += 1;
          return response({
            status: 403,
            headers: { "X-Workshop-Error-Code": "wrong_seat_identity" },
            json: { detail: "This workshop instance is assigned to alice@example.com." },
          });
        },
        () => fresh.api.exportCode(),
      ),
      (error: unknown) => error instanceof fresh.ApiError && error.status === 403,
    );
    assert.deepEqual(details, ["This workshop instance is assigned to alice@example.com."]);
    assert.equal(fresh.isWrongIdentityLocked(), true);

    await assert.rejects(
      withFetch(async () => {
        networkCalls += 1;
        return response({});
      }, () => fresh.api.exportCode()),
      (error: unknown) => error instanceof fresh.ApiError && error.status === 403,
    );
    assert.equal(networkCalls, 1);
  } finally {
    off();
  }
});

test("a ZIP that stops arriving halfway is a named transfer interruption", async () => {
  const broken = response({
    headers: { "Content-Disposition": 'attachment; filename="a.zip"' },
    blob: async () => { throw new TypeError("network error"); },
  });
  await assert.rejects(withFetch(async () => broken, () => api.exportCode()), (error: unknown) =>
    error instanceof ExportInterruptedError && error.kind === "transfer" && error.message === TRANSFER_INTERRUPTED_MESSAGE);
});

test("a non-network failure is not disguised as a sign-in problem", () => {
  const boom = new RangeError("bad input");
  assert.equal(exportTransportFailure(boom), boom);
  assert.equal(exportBodyFailure(boom), boom);
  assert.ok(exportTransportFailure(new TypeError("Failed to fetch")) instanceof ExportInterruptedError);
  assert.equal((exportBodyFailure(new TypeError("x")) as ExportInterruptedError).kind, "transfer");
});

test("only redirect-shaped responses count as a sign-in hand-off", () => {
  assert.equal(isSignInRedirect({ type: "opaqueredirect", redirected: false, status: 0 }), true);
  assert.equal(isSignInRedirect({ type: "basic", redirected: true, status: 200 }), true);
  assert.equal(isSignInRedirect({ type: "basic", redirected: false, status: 302 }), true);
  assert.equal(isSignInRedirect({ type: "basic", redirected: false, status: 200 }), false);
  assert.equal(isSignInRedirect({ type: "basic", redirected: false, status: 503 }), false);
});

test("saving a download clicks the link first and keeps its URL alive for a while after", () => {
  const calls: string[] = [];
  const timers: Array<{ run: () => void; delay: number }> = [];
  const env: DownloadEnv = {
    createObjectURL: () => {
      calls.push("create");
      return "blob:export";
    },
    revokeObjectURL: (url) => calls.push(`revoke ${url}`),
    click: (url, filename) => calls.push(`click ${url} ${filename}`),
    later: (run, delay) => timers.push({ run, delay }),
  };
  saveDownload(new Blob(["zip"]), "workshop-code-1.zip", env);
  assert.deepEqual(calls, ["create", "click blob:export workshop-code-1.zip"]);
  assert.equal(timers.length, 1);
  assert.equal(timers[0].delay, REVOKE_DELAY_MS);
  assert.ok(REVOKE_DELAY_MS >= 30_000, "revoking at once can cancel a save that has not started");
  timers[0].run();
  assert.equal(calls.at(-1), "revoke blob:export");
});
