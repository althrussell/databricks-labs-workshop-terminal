import assert from "node:assert/strict";
import test from "node:test";
import { terminalExitNotice } from "./terminalExit.ts";

test("failed startup preserves a useful exit-code message", () => {
  assert.equal(
    terminalExitNotice({ reason: "process_error", exit_code: 17 }),
    "Session ended with exit code 17. Scroll up for details. Select Relaunch to retry."
  );
});

test("a signal and an idle timeout offer distinct recovery messages", () => {
  assert.match(terminalExitNotice({ exit_signal: 15 }), /stopped by signal 15/);
  assert.match(terminalExitNotice({ reason: "idle_reaped" }), /after being idle/);
});

test("clean and older exit frames still offer relaunch without a failure claim", () => {
  assert.equal(terminalExitNotice({ exit_code: 0 }), "Session ended. Select Relaunch to start again.");
  assert.equal(terminalExitNotice({}), "Session ended. Select Relaunch to start again.");
  assert.match(terminalExitNotice({ reason: "process_error" }), /stopped unexpectedly/);
});

test("unexpected server prose is never printed into the terminal notice", () => {
  assert.equal(terminalExitNotice({ reason: "\u001b[2Juntrusted output" }), "Session ended. Select Relaunch to start again.");
});
