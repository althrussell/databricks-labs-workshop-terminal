import assert from "node:assert/strict";
import test from "node:test";
import { terminalExitNotice } from "./terminalExit.ts";

test("normal workshop closeout never offers relaunch, even when drain sent a signal", () => {
  assert.equal(terminalExitNotice({ reason: "workshop_closing", exit_signal: 15 }),
    "This workshop is closing. New input is disabled.");
});

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

test("a crash names the CLI's own resume command so the conversation is not lost", () => {
  assert.match(terminalExitNotice({ exit_signal: 9, agent: "claude" }), /type \/resume/);
  assert.match(terminalExitNotice({ reason: "process_error", exit_code: 1, agent: "codex" }), /codex resume/);
  assert.match(terminalExitNotice({ reason: "idle_reaped", agent: "claude" }), /\/resume/);
});

test("unknown agents and clean exits carry no resume hint", () => {
  assert.doesNotMatch(terminalExitNotice({ exit_signal: 9, agent: "omnigent" }), /resume/);
  assert.doesNotMatch(terminalExitNotice({ exit_signal: 9, agent: "\u001b[2Jevil" }), /evil/);
  assert.equal(terminalExitNotice({ exit_code: 0, agent: "claude" }), "Session ended. Select Relaunch to start again.");
});
