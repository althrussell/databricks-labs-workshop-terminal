/** Attendee lifecycle deliberately excludes operator closeout diagnostics. */
export type WorkshopState = "open" | "closing" | "closed";

export interface WorkshopStatus {
  state: WorkshopState;
  accepting_input: boolean;
}

const order: Record<WorkshopState, number> = { open: 0, closing: 1, closed: 2 };

/** Older in-flight reads must never re-enable a workshop after its barrier. */
export function advanceWorkshopState(current: WorkshopState | null, next: WorkshopState): WorkshopState {
  return current && order[current] > order[next] ? current : next;
}

export function workshopNotice(state: WorkshopState): string {
  return state === "closed"
    ? "This workshop has ended. New input is disabled. You can still view the resources on this page."
    : "This workshop is closing. New input is disabled. Please wait for your workshop host.";
}

const closingListeners = new Set<() => void>();

export function notifyWorkshopClosing(): void {
  closingListeners.forEach((listener) => listener());
}

export function onWorkshopClosing(listener: () => void): () => void {
  closingListeners.add(listener);
  return () => closingListeners.delete(listener);
}

export function isWorkshopClosingDetail(detail: unknown): boolean {
  return !!detail && typeof detail === "object"
    && (detail as { code?: unknown }).code === "workshop_closing";
}
