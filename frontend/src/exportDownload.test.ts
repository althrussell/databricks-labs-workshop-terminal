import assert from "node:assert/strict";
import test from "node:test";
import {
  ExportInterruptedError,
  REVOKE_DELAY_MS,
  SIGN_IN_ENDED_MESSAGE,
  TRANSFER_INTERRUPTED_MESSAGE,
  readExportBlob,
  requestExport,
  saveDownload,
  type DownloadEnv,
} from "./exportDownload.ts";

function response(init: { status?: number; type?: string; redirected?: boolean }): Response {
  return { status: 200, type: "basic", redirected: false, ...init } as unknown as Response;
}

test("export asks for manual redirects so a sign-in hop is seen, not followed", async () => {
  const seen: Array<{ url: string; init?: RequestInit }> = [];
  const ok = response({ status: 200 });
  const result = await requestExport(async (url, init) => {
    seen.push({ url, init });
    return ok;
  });
  assert.equal(result, ok);
  assert.equal(seen.length, 1);
  assert.equal(seen[0].url, "/api/export");
  assert.equal(seen[0].init?.method, "POST");
  assert.equal(seen[0].init?.redirect, "manual");
});

test("an opaque redirect from an expired proxy session asks the attendee to reload", async () => {
  await assert.rejects(
    requestExport(async () => response({ status: 0, type: "opaqueredirect" })),
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
  await assert.rejects(requestExport(async () => response({ status: 302 })), ExportInterruptedError);
  await assert.rejects(requestExport(async () => response({ status: 200, redirected: true })), ExportInterruptedError);
});

test("the browser's bare 'Failed to fetch' from a refused cross-origin hop is named, not shown raw", async () => {
  await assert.rejects(
    requestExport(async () => {
      throw new TypeError("Failed to fetch");
    }),
    (error: unknown) => error instanceof ExportInterruptedError && error.message === SIGN_IN_ENDED_MESSAGE,
  );
});

test("a non-network failure is not disguised as a sign-in problem", async () => {
  const boom = new RangeError("bad input");
  await assert.rejects(requestExport(async () => { throw boom; }), (error: unknown) => error === boom);
});

test("a server error response is returned for the caller's own error handling", async () => {
  const failed = response({ status: 503 });
  assert.equal(await requestExport(async () => failed), failed);
});

test("a ZIP that stops arriving halfway is a named transfer interruption", async () => {
  const broken = {
    blob: async () => {
      throw new TypeError("network error");
    },
  };
  await assert.rejects(readExportBlob(broken), (error: unknown) =>
    error instanceof ExportInterruptedError && error.kind === "transfer" && error.message === TRANSFER_INTERRUPTED_MESSAGE);
});

test("a complete ZIP is read as a blob unchanged", async () => {
  const zip = new Blob([new Uint8Array(12 * 1024 * 1024)], { type: "application/zip" });
  const read = await readExportBlob({ blob: async () => zip });
  assert.equal(read.size, 12 * 1024 * 1024);
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
