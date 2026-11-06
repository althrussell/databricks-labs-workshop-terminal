/** Which dropped or chosen files can be attached to the prompt.
 *
 * The server stores PNG, JPEG, WebP and GIF; anything else would be saved with
 * a misleading extension, so it is turned away here with a reason instead.
 */

export const ATTACHABLE_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"] as const;

/** For the file picker's `accept`, so it offers only what the server keeps. */
export const ATTACHABLE_ACCEPT = ATTACHABLE_TYPES.join(",");

interface FileLike {
  type: string;
}

export function splitAttachable<T extends FileLike>(files: readonly T[]): { images: T[]; rejected: number } {
  const images = files.filter((file) => (ATTACHABLE_TYPES as readonly string[]).includes(file.type));
  return { images, rejected: files.length - images.length };
}

/** What to say when some of a drop could not be attached; "" when all of it was. */
export function rejectedMessage(images: number, rejected: number): string {
  if (!rejected) return "";
  const what = rejected === 1 ? "That file isn't" : `${rejected} files aren't`;
  const kept = images ? ` The ${images === 1 ? "image was" : `${images} images were`} attached.` : "";
  return `${what} an image the agent can read: use PNG, JPG, WebP or GIF.${kept}`;
}

/** Only drags that carry files light up the zone; dragging selected text doesn't. */
export function dragCarriesFiles(types: readonly string[] | undefined): boolean {
  return !!types && Array.from(types).includes("Files");
}
