import { Award, Download, ExternalLink, Link as LinkIcon, Rocket, Settings2, ShieldCheck } from "lucide-react";
import type { ShellLink } from "../api";
import type { NetworkCheckResult, NetworkStatusKind } from "../networkCheck";
import { linkIcon } from "./linkIcons";
import { NetworkStatusControl } from "./NetworkNotice";

interface Props {
  links: ShellLink[];
  workspaceUrl: string;
  /** Empty when the event has no Omnigent app. */
  omnigentUrl: string;
  isAdmin: boolean;
  inputDisabled: boolean;
  canDownload: boolean;
  exportBusy: boolean;
  networkStatus: NetworkStatusKind;
  networkResult: NetworkCheckResult | null;
  onRunNetworkCheck: () => void;
  onPreferences: () => void;
  onDownload: () => void;
  onCertificate: () => void;
  onOperator: () => void;
}

/** Everything the laptop header holds that is not a phone tab, at the foot of
 * the Build tab: the same handlers, one row each. */
export default function PhoneMoreList(props: Props) {
  const { links, workspaceUrl, omnigentUrl, isAdmin, inputDisabled, canDownload, exportBusy } = props;
  const external = (url: string, label: string, Icon: typeof LinkIcon, highlight = false) => (
    <a key={url} className={`phone-more-row${highlight ? " phone-more-highlight" : ""}`} href={url} target="_blank" rel="noopener noreferrer">
      <Icon size={18} aria-hidden="true" />
      <span>{label}</span>
      <ExternalLink size={14} aria-hidden="true" className="phone-more-end" />
    </a>
  );
  return (
    <section className="phone-card phone-more" aria-label="More">
      <h2 className="phone-card-title">More</h2>
      <button type="button" className="phone-more-row" disabled={inputDisabled} onClick={props.onPreferences}>
        <Settings2 size={18} aria-hidden="true" />
        <span>Agent preferences</span>
      </button>
      {canDownload && (
        <button type="button" className="phone-more-row" disabled={inputDisabled || exportBusy} onClick={props.onDownload}>
          <Download size={18} aria-hidden="true" />
          <span>{exportBusy ? "Exporting…" : "Download my code"}</span>
        </button>
      )}
      <button type="button" className="phone-more-row" disabled={inputDisabled} onClick={props.onCertificate}>
        <Award size={18} aria-hidden="true" />
        <span>Certificate</span>
      </button>
      {links.filter((link) => link.highlight).map((link) => external(link.url, link.label, linkIcon(link.icon), true))}
      {workspaceUrl && external(workspaceUrl, "Workspace", LinkIcon)}
      {omnigentUrl && external(omnigentUrl, "Omnigent", Rocket)}
      {links.filter((link) => !link.highlight).map((link) => external(link.url, link.label, linkIcon(link.icon)))}
      <div className="phone-more-row phone-more-network">
        <span>Network</span>
        <span className="phone-more-end">
          <NetworkStatusControl
            status={props.networkStatus}
            result={props.networkResult}
            onRunAgain={props.onRunNetworkCheck}
            placement="auto"
          />
        </span>
      </div>
      {isAdmin && (
        <button type="button" className="phone-more-row" onClick={props.onOperator}>
          <ShieldCheck size={18} aria-hidden="true" />
          <span>Operator</span>
        </button>
      )}
    </section>
  );
}
