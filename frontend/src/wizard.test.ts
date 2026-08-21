import { test } from "node:test";
import assert from "node:assert/strict";
import {
  humanIndustry,
  INDUSTRY_CHIPS_ATTR,
  industryFromOther,
  industryOf,
  isGenericIdea,
  otherBlurCommits,
} from "./wizard.ts";
import type { WizardIdea } from "./api.ts";

function idea(extra: Partial<WizardIdea> = {}): WizardIdea {
  return {
    id: "x",
    label: "x",
    outcome: "x",
    prompt: "x",
    industries: [],
    intents: [],
    products: [],
    shape: "dashboard",
    technical: false,
    demo_tables: [],
    ...extra,
  };
}

test("humanIndustry title-cases a slug without the Fs hack", () => {
  assert.equal(humanIndustry("financial_services"), "Financial Services");
  assert.equal(humanIndustry("automotive_mobility"), "Automotive Mobility");
});

test("pickIdea copies the tagged industry", () => {
  assert.equal(
    industryOf(idea({ industries: ["retail"], demo_tables: ["retail.orders"] })),
    "retail"
  );
});

test("pickIdea falls back to the demo table schema", () => {
  assert.equal(
    industryOf(idea({ demo_tables: ["healthcare.encounters"] })),
    "healthcare"
  );
});

test("a generic card does not steer at a schema", () => {
  const generic = idea({ id: "generic-first-pipeline" });
  assert.equal(industryOf(generic), "");
  assert.equal(isGenericIdea(generic), true);
});

test("an empty Other box does not take away a chip the attendee picked", () => {
  // Opening Other to look at it, then clicking away, said nothing about the
  // chip — but blur fired and cleared it.
  assert.equal(industryFromOther("", "automotive_mobility", false), null);
  assert.equal(industryFromOther("   ", "automotive_mobility", false), null);
});

test("an empty Other box does clear an industry that was typed into it", () => {
  assert.equal(industryFromOther("", "taxidermy", true), "");
});

test("a typed industry replaces whatever was selected", () => {
  assert.equal(industryFromOther(" Taxidermy ", "", false), "Taxidermy");
  assert.equal(
    industryFromOther("taxidermy", "automotive_mobility", false),
    "taxidermy"
  );
});

test("typing an industry then pressing a chip lets the chip win", () => {
  // Blur runs before the click. Committing the abandoned text would put two
  // industry requests in flight and let the network decide which one stuck.
  const chip = {
    closest: (selector: string) =>
      selector === `[${INDUSTRY_CHIPS_ATTR}]` ? { tag: "div" } : null,
  };
  assert.equal(otherBlurCommits(chip), false);
});

test("blurring anywhere else still commits what was typed", () => {
  // Clicking Continue, or into the textarea, is the ordinary commit path.
  assert.equal(otherBlurCommits({ closest: () => null }), true);
  assert.equal(otherBlurCommits(null), true);
  // Clicking outside the document entirely reports no related target at all.
  assert.equal(otherBlurCommits(undefined), true);
});

test("an unchanged value does not re-request the grid", () => {
  // Blur fires on every click away, and refetching would replace the cards
  // under someone who was reading them.
  assert.equal(industryFromOther("taxidermy", "taxidermy", true), null);
  assert.equal(industryFromOther("", "", false), null);
});
