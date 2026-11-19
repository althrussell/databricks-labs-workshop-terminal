import type { PointerEvent, RefObject } from "react";
import { Keyboard } from "lucide-react";
import { TOUCH_KEYS } from "../touchKeys";
import type { TerminalHandle } from "./TerminalView";

interface Props {
  terminal: RefObject<TerminalHandle | null>;
}

/** Keeps focus (and an open soft keyboard) where it was when a key is tapped. */
const keepFocus = (event: PointerEvent) => event.preventDefault();

/** The keys a phone keyboard lacks, for answering an agent's menus by touch. */
export default function TouchKeyBar({ terminal }: Props) {
  return (
    <div className="touch-keys" role="toolbar" aria-label="Terminal keys">
      {TOUCH_KEYS.map(({ key, label, name }) => (
        <button
          key={key}
          type="button"
          className="touch-key"
          aria-label={name}
          title={name}
          onPointerDown={keepFocus}
          onClick={() => terminal.current?.press(key)}
        >
          {label}
        </button>
      ))}
      <button
        type="button"
        className="touch-key"
        aria-label="Type in the terminal"
        title="Type in the terminal"
        onClick={() => terminal.current?.openKeyboard()}
      >
        <Keyboard size={16} aria-hidden="true" />
      </button>
    </div>
  );
}
