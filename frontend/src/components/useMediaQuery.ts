import { useEffect, useState } from "react";

/** Phones and tablets: no hover, and fingers rather than a mouse. */
export const TOUCH_QUERY = "(pointer: coarse)";

/** A phone, held either way: a touch screen whose short side is small. It gets
 * the tab-bar shell (data-shell="phone"); tablets and laptops keep the
 * desktop layout. The height clause catches a phone turned sideways. */
export const PHONE_QUERY = "(pointer: coarse) and (max-width: 700px), (pointer: coarse) and (max-height: 500px)";

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
