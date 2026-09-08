import assert from "node:assert/strict";
import test from "node:test";

import { terminalGatewayLimit } from "./gatewayLimit";

test("a definitive Claude budget 403 is surfaced as exhausted allowance", () => {
  assert.equal(
    terminalGatewayLimit("API Error: 403 budget policy limit exceeded"),
    "gateway_allowance_exhausted"
  );
});

test("ANSI and fragmented terminal output can be classified from a rolling window", () => {
  assert.equal(
    terminalGatewayLimit("\x1b[31mError 403: AI Gateway bud" + "get exhausted\x1b[0m"),
    "gateway_allowance_exhausted"
  );
});

test("temporary gateway pressure remains distinct from a spent budget", () => {
  assert.equal(
    terminalGatewayLimit("429 Too Many Requests: TPM exceeded; rate limit"),
    "gateway_rate_limited"
  );
  assert.equal(
    terminalGatewayLimit("Too Many Requests: retry after 30 seconds"),
    "gateway_rate_limited"
  );
});

test("an explicit rate denial wins over unrelated nearby budget prose", () => {
  assert.equal(
    terminalGatewayLimit(
      "Project budget remaining: $20\r\n429 Too Many Requests: TPM exceeded"
    ),
    "gateway_rate_limited"
  );
});

test("ordinary terminal prose cannot create a false budget banner", () => {
  assert.equal(terminalGatewayLimit("Our project budget is $100"), null);
  assert.equal(terminalGatewayLimit("HTTP 403 from a storage API"), null);
  assert.equal(
    terminalGatewayLimit("403 from storage" + ".".repeat(900) + "project budget"),
    null
  );
});

test("gateway machine-readable spend codes are recognised", () => {
  assert.equal(
    terminalGatewayLimit("429 service_spend_limit_reached"),
    "gateway_allowance_exhausted"
  );
});
