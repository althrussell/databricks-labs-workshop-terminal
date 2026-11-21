import { useEffect, useState } from "react";
import { PHONE_MAX_LANDSCAPE_HEIGHT, PHONE_MAX_WIDTH } from "../viewport";

/** Phones and tablets: no hover, and fingers rather than a mouse. */
export const TOUCH_QUERY = "(pointer: coarse)";

/** A phone, held either way: a touch screen whose short side is small. It gets
 * the tab-bar shell (data-shell="phone"); tablets and laptops keep the
 * desktop layout. Used for the initial/fallback check; live resize tracking in
 * App.tsx uses `isPhoneViewport` with `KeyboardTracker.fullHeight` so opening a
 * soft keyboard on a landscape tablet never flips shells. */
export const PHONE_QUERY = `(pointer: coarse) and (max-width: ${PHONE_MAX_WIDTH}px), (pointer: coarse) and (max-height: ${PHONE_MAX_LANDSCAPE_HEIGHT}px)`;

/** Whether `query` matches now, kept current as the window or device changes. */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const list = window.matchMedia(query);
    const update = () => setMatches(list.matches);
    update();
    list.addEventListener("change", update);
    return () => list.removeEventListener("change", update);
  }, [query]);
  return matches;
}
