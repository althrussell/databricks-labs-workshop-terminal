// Polled reads (api.ts `polled`) retry a 502 quietly (NF-01, 30 Oct: seven 502s
// on /api/agents, /api/projects and /api/workshop-status, each recovered on the
// next poll, so the attendee saw an error for a read that worked moments later).
// Only a 502 is retried: a 4xx or 5xx from the app is a real answer.

export interface RetryOptions {
  /** Total tries, including the first. */
  attempts?: number;
  /** Wait before each retry, in order; the last value repeats. */
  delaysMs?: readonly number[];
  sleep?: (ms: number) => Promise<void>;
  isRetryable?: (error: unknown) => boolean;
}

/** True for an error that carries HTTP status 502 (the proxy's bad gateway). */
export function isBadGateway(error: unknown): boolean {
  return typeof error === "object" && error !== null && (error as { status?: unknown }).status === 502;
}

/** Run ``run``, retrying retryable failures after short waits. Re-throws the last error. */
export async function retryQuietly<T>(run: () => Promise<T>, options: RetryOptions = {}): Promise<T> {
  const attempts = Math.max(1, options.attempts ?? 3);
  const delays = options.delaysMs?.length ? options.delaysMs : [300, 900];
  const sleep = options.sleep ?? ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)));
  const retryable = options.isRetryable ?? isBadGateway;
  for (let attempt = 1; ; attempt += 1) {
    try {
      return await run();
    } catch (error) {
      if (attempt >= attempts || !retryable(error)) throw error;
      await sleep(delays[Math.min(attempt - 1, delays.length - 1)]);
    }
  }
}
