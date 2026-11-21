import { CircleQuestionMark, Hammer, Lightbulb, SquareTerminal } from "lucide-react";

export type PhoneTab = "terminal" | "build" | "tips" | "help";

const TABS = [
  { tab: "terminal", label: "Terminal", Icon: SquareTerminal },
  { tab: "build", label: "Build", Icon: Hammer },
  { tab: "tips", label: "Tips", Icon: Lightbulb },
  { tab: "help", label: "Help", Icon: CircleQuestionMark },
] as const;

interface Props {
  tab: PhoneTab;
  helpUnread: number;
  /** Tips can be switched off for an event (shell.features.nuggets_pane). */
  showTips: boolean;
  onSelect: (tab: PhoneTab) => void;
}

/** The phone shell's navigation, in the thumb zone; a left rail in landscape. */
export default function PhoneTabBar({ tab, helpUnread, showTips, onSelect }: Props) {
  return (
    <nav className="phone-tabs" aria-label="Workshop sections">
      {TABS.filter((item) => showTips || item.tab !== "tips").map(({ tab: id, label, Icon }) => {
        const unread = id === "help" && helpUnread > 0;
        return (
          <button
            key={id}
            type="button"
            className="phone-tab"
            aria-current={tab === id ? "page" : undefined}
            aria-label={unread ? `${label}, ${helpUnread} unread` : label}
            onClick={() => onSelect(id)}
          >
            <span className="phone-tab-icon">
              <Icon size={20} aria-hidden="true" />
              {unread && <span className="phone-tab-badge" aria-hidden="true">{helpUnread}</span>}
            </span>
            <span className="phone-tab-label" aria-hidden="true">{label}</span>
          </button>
        );
      })}
    </nav>
  );
}
