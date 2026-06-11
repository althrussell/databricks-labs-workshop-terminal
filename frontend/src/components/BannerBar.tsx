import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, Info, X } from "lucide-react";
import { Broadcast } from "../api";
import { onAppEvent } from "../events";

const ICONS = { info: Info, success: CheckCircle2, warning: AlertTriangle };

export default function BannerBar({ initial }: { initial: Broadcast | null }) {
  const [banner, setBanner] = useState<Broadcast | null>(initial);

  useEffect(() => {
    setBanner(initial);
  }, [initial]);

  useEffect(() => {
    const off = onAppEvent((event) => {
      if (event.t === "broadcast") {
        setBanner({ message: event.message, level: event.level as Broadcast["level"], ttl_s: event.ttl_s });
      }
    });
    return off;
  }, []);

  useEffect(() => {
    if (!banner) return;
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
