import { useEffect, useRef, useState } from "react";
import { ImagePlus } from "lucide-react";
import { ATTACHABLE_ACCEPT, dragCarriesFiles } from "../imageDrop";

interface Props {
  busy: boolean;
  disabled: boolean;
  title: string;
  onFiles: (files: File[]) => void;
}

/** "Attach image" as a drop zone: click to choose a file, or drag one in.
 *
 * While a file is dragged anywhere over the page the zone lights up to show
 * where it goes. A drop that misses it still attaches: otherwise the browser
 * opens the image in this tab and the attendee loses the terminal.
 */
export default function ImageDropZone({ busy, disabled, title, onFiles }: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const onFilesRef = useRef(onFiles);
  onFilesRef.current = onFiles;

  useEffect(() => {
    if (disabled) return;
    // dragover repeats while a drag is over the page and stops when it leaves,
    // which is steadier than pairing dragenter/dragleave across child elements.
    let idle = 0;
    const onDragOver = (event: DragEvent) => {
      if (!dragCarriesFiles(event.dataTransfer?.types)) return;
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
      setDragging(true);
      window.clearTimeout(idle);
      idle = window.setTimeout(() => setDragging(false), 250);
    };
    const onDrop = (event: DragEvent) => {
      if (!dragCarriesFiles(event.dataTransfer?.types)) return;
      event.preventDefault();
      window.clearTimeout(idle);
      setDragging(false);
      const files = Array.from(event.dataTransfer?.files ?? []);
      if (files.length) onFilesRef.current(files);
    };
    window.addEventListener("dragover", onDragOver);
    window.addEventListener("drop", onDrop);
    return () => {
      window.clearTimeout(idle);
      window.removeEventListener("dragover", onDragOver);
      window.removeEventListener("drop", onDrop);
      setDragging(false);
    };
  }, [disabled]);

  const label = busy ? "Attaching…" : dragging ? "Drop image here" : "Attach image";
  return (
    <>
      <input
        ref={inputRef}
        type="file"
        accept={ATTACHABLE_ACCEPT}
        multiple
        style={{ display: "none" }}
        onChange={(event) => {
          const files = Array.from(event.target.files ?? []);
          event.target.value = "";
          if (files.length) onFiles(files);
        }}
      />
      <button
        type="button"
        className={`btn btn-ghost image-drop${dragging ? " image-drop-active" : ""}`}
        disabled={disabled || busy}
        title={title}
        onClick={() => inputRef.current?.click()}
      >
        <ImagePlus size={14} aria-hidden="true" /> {label}
      </button>
    </>
  );
}
