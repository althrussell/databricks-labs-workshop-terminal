/** The part of a phone screen the app can use, and whether the keyboard is up.
 *
 * `100vh` on a phone ignores both the browser's toolbars and the soft keyboard,
 * so the bottom of the app (the prompt box, the terminal's last rows) slides
 * under them. The visual viewport is the visible area; sizing the app to it and
 * dropping the top bars while the keyboard is open keeps the prompt in view.
 */

/** Below this share of the tallest height seen at this width, a keyboard is up. */
const KEYBOARD_SHARE = 0.75;

export class KeyboardTracker {
  private width = 0;
  private tallest = 0;

  /** Feed each visual-viewport size; returns whether the keyboard looks open. */
  update(width: number, height: number): boolean {
    // A rotation or a resized window changes what "full height" means.
    if (Math.round(width) !== Math.round(this.width)) {
      this.width = width;
      this.tallest = height;
    }
    this.tallest = Math.max(this.tallest, height);
    return height < this.tallest * KEYBOARD_SHARE;
  }
}

export interface VisualViewportLike {
  readonly width: number;
  readonly height: number;
  readonly offsetTop: number;
  readonly scale?: number;
}

export interface ViewportUpdate {
  appHeightPx: number;
  keyboardOpen: boolean;
  resetScroll: boolean;
}

/** Decide how to update `--app-height`, `data-keyboard` and page scroll; null while pinch-zoomed. */
export function measureViewport(
  tracker: KeyboardTracker,
  vv: VisualViewportLike,
  scrollY: number,
  touch: boolean
): ViewportUpdate | null {
  // Pinch-zooming shrinks vv.height and sets vv.offsetTop > 0: leave layout and panning alone.
  if ((vv.scale ?? 1) > 1.01) return null;
  return {
    appHeightPx: Math.round(vv.height),
    keyboardOpen: touch && tracker.update(vv.width, vv.height),
    resetScroll: vv.offsetTop > 0 || scrollY > 0,
  };
}
