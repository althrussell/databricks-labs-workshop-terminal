import { useEffect, useState } from "react";

/** Phones and tablets: no hover, and fingers rather than a mouse. */
export const TOUCH_QUERY = "(pointer: coarse)";

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
