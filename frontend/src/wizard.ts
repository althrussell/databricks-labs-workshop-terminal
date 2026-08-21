import type { WizardIdea } from "./api";

/** Title-case a schema slug. ``financial_services`` → ``Financial Services``. */
export function humanIndustry(value: string): string {
  return value
    .split("_")
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

/** The schema a card belongs to, if it belongs to one.
 *
 * Tagged industries win; otherwise the schema of the first demo table. A
 * generic card (no tags, no tables) returns empty — picking it is a choice
 * not to steer the agent at a schema.
 */
export function industryOf(idea: WizardIdea): string {
  if (idea.industries.length > 0) return idea.industries[0];
  const first = idea.demo_tables[0];
  if (!first) return "";
  const schema = first.split(".")[0] ?? "";
  return schema;
}

/** Untagged cards that name no demo tables — they work with any data. */
export function isGenericIdea(idea: WizardIdea): boolean {
  return idea.industries.length === 0 && idea.demo_tables.length === 0;
}

/** What the free-text industry box should change the industry to, or null.
 *
 * Null means leave it alone, which is most of the time. The box commits on
 * blur, and blur fires on every click away — including the click that lands on
 * a chip — so the two cases it must stay quiet for are a value that has not
 * changed, and an empty box while the industry belongs to a chip. Clearing on
 * that second case took the attendee's selected industry away for the crime of
 * opening the box to look at it.
 *
 * `ownedByOther` is whether the current industry was typed here rather than
 * picked from a chip. Only then does emptying the box mean "remove it".
 */
export function industryFromOther(
  value: string,
  current: string,
  ownedByOther: boolean,
  offered: readonly string[] = [],
): string | null {
  const next = slugifyIndustry(value, offered);
  if (!next && !ownedByOther) return null;
  return next === current ? null : next;
}

/** A typed industry as a schema slug, mirroring `demo_data.industry_slug`.
 *
 * The free-text box is the only place an industry enters as prose, and every
 * consumer downstream compares slugs: the seeded set, the chip list, the label
 * map. Keeping the raw text meant "Financial Services" matched none of them, so
 * an attendee who typed the full name of a seeded industry was told there was
 * no demo data for it and left sitting on Other next to the chip they had just
 * described. The server slugs it too, and does so idempotently, so normalising
 * here only decides what the attendee is shown.
 */
export function slugifyIndustry(
  value: string,
  offered: readonly string[] = [],
): string {
  const slug = value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  if (!slug) return "";
  const compact = slug.replace(/_/g, "");
  for (const known of offered) {
    if (known.replace(/_/g, "") === compact) return known;
  }
  return slug;
}

/** Marks the chip row, so a blur landing inside it can be recognised. */
export const INDUSTRY_CHIPS_ATTR = "data-industry-chips";

/** Whether a blur out of the free-text box should commit what was typed.
 *
 * Not when the click that caused it is landing on a chip. Blur runs before the
 * click, so typing an industry and then deciding against it by pressing a chip
 * would otherwise commit the abandoned text first and the chip second: two
 * industry requests in flight at once, settling in whichever order the network
 * returned them. The chip is the later and more deliberate gesture, so the box
 * stays quiet and lets it through.
 */
export function otherBlurCommits(relatedTarget: unknown): boolean {
  const el = relatedTarget as { closest?: (selector: string) => unknown } | null;
  if (!el || typeof el.closest !== "function") return true;
  return el.closest(`[${INDUSTRY_CHIPS_ATTR}]`) == null;
}
