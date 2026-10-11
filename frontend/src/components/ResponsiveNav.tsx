import { useLayoutEffect, useRef, useState, type ReactNode } from "react";

/** Use the real link widths, including configured labels and session controls. */
export default function ResponsiveNav({ children }: { children: ReactNode }) {
  const navRef = useRef<HTMLElement>(null);
  const itemsRef = useRef<HTMLDivElement>(null);
  const [compact, setCompact] = useState(true);

  useLayoutEffect(() => {
    const nav = navRef.current;
    const items = itemsRef.current;
    if (!nav || !items) return;
    let frame = 0;
    let disposed = false;
    const measure = () => {
      const controls = Array.from(items.children);
      const gap = parseFloat(getComputedStyle(items).columnGap) || 0;
      let expandedWidth = controls.reduce((sum, item) => {
        const style = getComputedStyle(item);
        return sum + item.getBoundingClientRect().width
          + (parseFloat(style.marginLeft) || 0) + (parseFloat(style.marginRight) || 0);
      }, 0)
        + gap * Math.max(0, controls.length - 1);
      // Hidden labels keep their intrinsic width, so measuring in compact mode
      // does not oscillate between icons and text at the same viewport width.
      if (nav.dataset.compact === "true") {
        items.querySelectorAll<HTMLElement>(".nav-label").forEach((label) => {
          expandedWidth += label.getBoundingClientRect().width
            + (parseFloat(getComputedStyle(label.parentElement!).columnGap) || 0);
        });
      }
      setCompact(expandedWidth > nav.clientWidth + 0.5);
    };
    const schedule = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(measure);
    };
    measure();
    const observer = new ResizeObserver(schedule);
    observer.observe(nav);
    for (const item of items.children) observer.observe(item);
    void document.fonts.ready.then(() => { if (!disposed) schedule(); });
    return () => { disposed = true; observer.disconnect(); cancelAnimationFrame(frame); };
  }, [children]);

  return <nav ref={navRef} className="header-links" aria-label="Workshop navigation" data-compact={compact}>
    <div ref={itemsRef} className="header-link-items">{children}</div>
  </nav>;
}
