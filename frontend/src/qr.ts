/** A QR code for moving this workshop from a laptop to a phone. */
import qrcode from "qrcode-generator";

/** The app's home: the same seat, signed in as the same person, on any device. */
export function phoneUrl(loc: { origin: string }): string {
  return `${loc.origin}/`;
}

export interface QrPath {
  /** Modules per side, without the quiet zone. */
  size: number;
  /** One SVG path of every dark module, in module units. */
  d: string;
}

/** Medium error correction survives a glare or a slightly blurred camera. */
export function qrPath(text: string): QrPath {
  const code = qrcode(0, "M");
  code.addData(text);
  code.make();
  const size = code.getModuleCount();
  const runs: string[] = [];
  for (let row = 0; row < size; row += 1) {
    let col = 0;
    while (col < size) {
      if (!code.isDark(row, col)) {
        col += 1;
        continue;
      }
      const start = col;
      while (col < size && code.isDark(row, col)) col += 1;
      runs.push(`M${start} ${row}h${col - start}v1h${start - col}z`);
    }
  }
  return { size, d: runs.join("") };
}
