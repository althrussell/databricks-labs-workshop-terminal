/** The part of a phone screen the app can use, and whether the keyboard is up.
 *
 * `100vh` on a phone ignores both the browser's toolbars and the soft keyboard,
 * so the bottom of the app (the prompt box, the terminal's last rows) slides
 * under them. The visual viewport is the visible area; sizing the app to it and
 * dropping the top bars while the keyboard is open keeps the prompt in view.
 */

/** Below this share of the tallest height seen at this width, a keyboard is up. */
const KEYBOARD_SHARE = 0.75;

/** Width / un-keyboarded landscape-height ceilings for the phone tab shell. */
export const PHONE_MAX_WIDTH = 700;
export const PHONE_MAX_LANDSCAPE_HEIGHT = 500;

export class KeyboardTracker {
  private width = 0;
  private tallest = 0;

  /** Tallest viewport height observed at the current width (before the keyboard). */
  get fullHeight(): number {
    return this.tallest;
  }

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

/** Whether a touch viewport should use the phone tab shell.
 *
 * Uses the un-keyboarded `fullHeight` (from `KeyboardTracker`) rather than the
 * live height, because `interactive-widget=resizes-content` shrinks the layout
 * viewport when the keyboard opens — otherwise a landscape tablet (e.g. 1024×768)
 * would flip into the phone shell mid-keystroke and unmount desktop inputs. */
export function isPhoneViewport(touch: boolean, width: number, fullHeight: number): boolean {
  return touch && (width <= PHONE_MAX_WIDTH || fullHeight <= PHONE_MAX_LANDSCAPE_HEIGHT);
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

/** Decide how to update `--app-height`, `data-keyboard` and page scroll.
 *
 * Pinch-zoom shrinks the visual viewport by its scale, so the size is scaled
 * back first: zooming neither resizes the app nor reads as a keyboard, while a
 * keyboard opened or closed during the zoom still counts. */
export function measureViewport(
  tracker: KeyboardTracker,
  vv: VisualViewportLike,
  scrollY: number,
  touch: boolean
): ViewportUpdate {
  const scale = Math.max(vv.scale ?? 1, 1);
  const zoomed = scale > 1.01;
  const width = vv.width * scale;
  const height = vv.height * scale;
  return {
    appHeightPx: Math.round(height),
    keyboardOpen: touch && tracker.update(width, height),
    // While zoomed, offsetTop is the attendee panning: leave it alone.
    resetScroll: !zoomed && (vv.offsetTop > 0 || scrollY > 0),
  };
}
