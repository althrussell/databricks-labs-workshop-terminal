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
): string | null {
  const next = value.trim();
  if (!next && !ownedByOther) return null;
  return next === current ? null : next;
}
