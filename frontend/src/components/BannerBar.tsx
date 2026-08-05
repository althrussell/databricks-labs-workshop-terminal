import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, Info, X } from "lucide-react";
import { Broadcast } from "../api";
import { onAppEvent } from "../events";

const ICONS = { info: Info, success: CheckCircle2, warning: AlertTriangle, error: AlertTriangle };

/** Pinned standing notice for the room.
 *
 * Banners are persistent state: they are shown because a condition holds and
 * cleared when it lifts, which is why the server retains them and replays them
 * on page load. They are deliberately *not* used for messages addressed to a
 * person — a single slot silently overwrites, and one timer cannot be right for
 * both a lunch announcement and an answer to a question. Those are toasts; see
 * `ToastHost`.
 *
 * The `suppressHelp` prop and the `source === "help"` sniffing that used to live
 * here existed only to work around operator replies being smuggled through this
 * component. With replies routed to toasts, both are gone.
 */
export default function BannerBar({ initial }: { initial: Broadcast | null }) {
  const [banner, setBanner] = useState<Broadcast | null>(initial);

  useEffect(() => {
    setBanner(initial);
  }, [initial]);

  useEffect(() => {
    const off = onAppEvent((event) => {
      if (event.t !== "broadcast") return;
      if (event.clear || !event.message.trim()) {
        setBanner(null);
        return;
      }
      // Toast-surface messages are not this component's business.
      if ((event.surface ?? "toast") !== "banner") return;
      setBanner({
        message: event.message,
        level: event.level as Broadcast["level"],
        ttl_s: event.ttl_s,
        surface: "banner",
        durability: event.durability,
      });
    });
    return off;
  }, []);

  useEffect(() => {
    if (!banner) return;
    // A sticky or critical notice holds until the condition lifts, so it carries
    // no dismissal timer. Only a transient notice expires on its own.
    if (banner.durability && banner.durability !== "transient") return;
    const timer = setTimeout(() => setBanner(null), banner.ttl_s * 1000);
    return () => clearTimeout(timer);
  }, [banner]);

  if (!banner) return null;
  const Icon = ICONS[banner.level] ?? Info;
  return (
    <div className={`banner banner-${banner.level}`}>
      <Icon size={16} />
      <span>{banner.message}</span>
      <button className="icon-btn" onClick={() => setBanner(null)}>
        <X size={14} />
      </button>
    </div>
  );
}
