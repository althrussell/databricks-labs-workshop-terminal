/** One owner for every candidate request. Abort alone is not a revision guard. */
export interface WizardRequest {
  revision: number;
  signal: AbortSignal;
}

export class WizardRequests {
  private revision = 0;
  private controller: AbortController | null = null;

  cancel() {
    this.controller?.abort();
    this.controller = null;
    this.revision += 1;
  }

  begin(): WizardRequest {
    this.cancel();
    this.controller = new AbortController();
    return { revision: this.revision, signal: this.controller.signal };
  }

  current(request: WizardRequest): boolean {
    return !request.signal.aborted && request.revision === this.revision;
  }
}

/** A saved task is loaded once per live session, including across page reloads. */
export async function wizardDeliveryId(sessionId: string, prompt: string): Promise<string> {
  const bytes = new TextEncoder().encode(sessionId + "\0" + prompt);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return "wizard:" + Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}
