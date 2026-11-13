// Getting an attendee's exported ZIP from Workshop Terminal into their browser.
//
// Two ways this fails for an attendee, and both used to read as "Failed to fetch":
//
// 1. The Apps proxy session has expired (or the connection dropped). The proxy answers with a 302 to the
//    workspace sign-in page on another origin. A plain fetch() follows that hop,
//    the cross-origin response carries no CORS header, and the browser reports
//    only a TypeError. Asking fetch for manual redirects turns the hop into an
//    opaque response this module can name (the same hazard server/main.py
//    documents for the keepalive iframe).
// 2. A saved file is revoked the moment it is clicked. Safari and Firefox can
//    cancel a blob download that has not started yet, so the URL is kept alive
//    for a while after the click.
//
// Pure where it can be, so the decisions are tested without a browser.

export const SIGN_IN_ENDED_MESSAGE =
  "The export did not reach Workshop Terminal. Your Databricks sign-in may have ended, " +
  "or the connection dropped. Reload this tab, then choose Download my code again. Your projects are unchanged.";

export const TRANSFER_INTERRUPTED_MESSAGE =
  "The download was interrupted before the ZIP finished arriving. Choose Download my code again.";

export type ExportInterruption = "sign_in" | "transfer";

export class ExportInterruptedError extends Error {
  readonly kind: ExportInterruption;

  constructor(kind: ExportInterruption) {
    super(kind === "sign_in" ? SIGN_IN_ENDED_MESSAGE : TRANSFER_INTERRUPTED_MESSAGE);
    this.name = "ExportInterruptedError";
    this.kind = kind;
  }
}

/** Whether a response is the proxy handing the attendee to a sign-in page. */
export function isSignInRedirect(resp: Pick<Response, "type" | "redirected" | "status">): boolean {
  return resp.type === "opaqueredirect" || resp.redirected || (resp.status >= 300 && resp.status < 400);
}

/**
 * What a failed request means. A refused redirect, or a request that never
 * completes, is reported as a sign-in interruption so the attendee is told to
 * reload instead of seeing "Failed to fetch". Anything else is rethrown as-is.
 * (The request itself is made in api.ts, the only HTTP module.)
 */
export function exportTransportFailure(error: unknown): unknown {
  if (error instanceof TypeError) return new ExportInterruptedError("sign_in");
  return error;
}

/** A body that stops arriving partway is a named transfer interruption. */
export function exportBodyFailure(error: unknown): unknown {
  if (error instanceof TypeError) return new ExportInterruptedError("transfer");
  return error;
}

export interface DownloadEnv {
  createObjectURL(blob: Blob): string;
  revokeObjectURL(url: string): void;
  click(url: string, filename: string): void;
  later(run: () => void, delayMs: number): void;
}

/** How long a saved file's object URL stays valid after the browser is asked to save it. */
export const REVOKE_DELAY_MS = 60_000;

export function browserDownloadEnv(): DownloadEnv {
  return {
    createObjectURL: (blob) => URL.createObjectURL(blob),
    revokeObjectURL: (url) => URL.revokeObjectURL(url),
    click: (url, filename) => {
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      link.style.display = "none";
      document.body.appendChild(link);
      link.click();
      link.remove();
    },
    later: (run, delayMs) => {
      setTimeout(run, delayMs);
    },
  };
}

/** Hand a downloaded blob to the browser's save flow and release it later. */
export function saveDownload(blob: Blob, filename: string, env: DownloadEnv = browserDownloadEnv()): void {
  const url = env.createObjectURL(blob);
  env.click(url, filename);
  env.later(() => env.revokeObjectURL(url), REVOKE_DELAY_MS);
}
