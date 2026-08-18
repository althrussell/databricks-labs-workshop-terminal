import { test } from "node:test";
import assert from "node:assert/strict";
import { humanIndustry, industryOf, isGenericIdea } from "./wizard.ts";
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
