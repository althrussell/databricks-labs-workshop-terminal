import assert from "node:assert/strict";
import test from "node:test";
import { WizardRequests, wizardDeliveryId } from "./wizardRequests";

test("editing immediately invalidates even a response that ignores abort", () => {
  const coordinator = new WizardRequests();
  const old = coordinator.begin();
  coordinator.cancel();
  assert.equal(coordinator.current(old), false);
  assert.equal(old.signal.aborted, true);
});

test("only the newest GET, generated suggestion or surprise can publish", () => {
  const coordinator = new WizardRequests();
  const curated = coordinator.begin();
  const generated = coordinator.begin();
  const surprise = coordinator.begin();
  assert.equal(coordinator.current(curated), false);
  assert.equal(coordinator.current(generated), false);
  assert.equal(coordinator.current(surprise), true);
});

test("repeated cancellation followed by a new request never revives an old ticket", () => {
  const coordinator = new WizardRequests();
  const old = coordinator.begin();
  coordinator.cancel();
  coordinator.cancel();
  const current = coordinator.begin();
  assert.equal(coordinator.current(old), false);
  assert.equal(coordinator.current(current), true);
});

test("reloaded wizard retries retain the same delivery while new tasks and sessions differ", async () => {
  const original = await wizardDeliveryId("session-one", "Help bakery staff pack orders");
  assert.equal(await wizardDeliveryId("session-one", "Help bakery staff pack orders"), original);
  assert.notEqual(await wizardDeliveryId("session-two", "Help bakery staff pack orders"), original);
  assert.notEqual(await wizardDeliveryId("session-one", "Help bakery staff plan a rota"), original);
});
