import { describe, expect, it } from "vitest";

import { describeErrorCode, hintForCode, isTemporaryCode } from "./errors";

describe("the words for AI tool calling failures", () => {
  it.each([
    ["TOOL_NOT_FOUND", "That tool is not available."],
    [
      "TOOL_APPROVAL_PENDING",
      "An action in this conversation is waiting for your approval. Approve or reject it first.",
    ],
    ["TOOL_APPROVAL_EXPIRED", "The action was not approved in time, so it expired and was not carried out."],
    ["TOOL_APPROVAL_STALE", "The action changed since it was shown to you. Review it again before deciding."],
    ["TOOL_APPROVAL_CLOSED", "This action is no longer waiting for a decision."],
  ])("describes %s", (code, text) => {
    expect(describeErrorCode(code)).toBe(text);
    // None of them goes away by trying the same thing again.
    expect(isTemporaryCode(code)).toBe(false);
    expect(hintForCode(code)).toBeNull();
  });

  it("keeps the general sentence for codes it does not know", () => {
    expect(describeErrorCode("SOMETHING_NEW")).toBe("The reply could not be generated.");
    expect(describeErrorCode(null)).toBe("The reply could not be generated.");
  });
});
