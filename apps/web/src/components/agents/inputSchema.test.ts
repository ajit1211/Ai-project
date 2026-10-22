import { describe, expect, it } from "vitest";

import { buildInput, humanize, initialValues, inputFormFor, parseJsonObject, type InputField } from "./inputSchema";

function fieldsOf(schema: Record<string, unknown>): InputField[] {
  const form = inputFormFor(schema);
  if (form.kind !== "fields") throw new Error(`expected fields, got ${form.kind}`);
  return form.fields;
}

describe("the input form of a schema", () => {
  it("asks for nothing without a schema, or for an object that allows no properties", () => {
    expect(inputFormFor(null)).toEqual({ kind: "none" });
    expect(inputFormFor({ type: "object", properties: {}, additionalProperties: false })).toEqual({ kind: "none" });
  });

  it("makes one field per simple property, with labels, bounds, required marks and defaults", () => {
    const fields = fieldsOf({
      type: "object",
      properties: {
        topic: { type: "string", title: "Subject", maxLength: 80, minLength: 2 },
        body: { type: "string", description: "  The text.  " },
        tone: { type: "string", enum: ["formal", "casual"], default: "casual" },
        level: { enum: [1, 2, 3] },
        maxItems: { type: "integer", minimum: 1, maximum: 10, default: 3 },
        ratio: { type: "number", exclusiveMinimum: 0, exclusiveMaximum: 1 },
        draft: { type: "boolean", default: true },
      },
      required: ["topic", "tone"],
    });
    expect(fields).toEqual([
      {
        name: "topic",
        label: "Subject",
        description: null,
        required: true,
        kind: "text",
        minLength: 2,
        maxLength: 80,
        multiline: false,
        initial: "",
      },
      {
        name: "body",
        label: "Body",
        description: "The text.",
        required: false,
        kind: "text",
        minLength: null,
        maxLength: null,
        multiline: true,
        initial: "",
      },
      {
        name: "tone",
        label: "Tone",
        description: null,
        required: true,
        kind: "choice",
        options: ["formal", "casual"],
        initial: "1",
      },
      { name: "level", label: "Level", description: null, required: false, kind: "choice", options: [1, 2, 3], initial: "" },
      {
        name: "maxItems",
        label: "Max items",
        description: null,
        required: false,
        kind: "number",
        integer: true,
        minimum: 1,
        maximum: 10,
        exclusiveMinimum: null,
        exclusiveMaximum: null,
        initial: "3",
      },
      {
        name: "ratio",
        label: "Ratio",
        description: null,
        required: false,
        kind: "number",
        integer: false,
        minimum: null,
        maximum: null,
        exclusiveMinimum: 0,
        exclusiveMaximum: 1,
        initial: "",
      },
      { name: "draft", label: "Draft", description: null, required: false, kind: "boolean", initial: true },
    ]);
    expect(initialValues(fields)).toEqual({
      topic: "",
      body: "",
      tone: "1",
      level: "",
      maxItems: "3",
      ratio: "",
      draft: true,
    });
  });

  it("falls back to JSON for anything plain fields cannot express", () => {
    const json = { kind: "json" };
    expect(inputFormFor({ type: "array" })).toEqual(json);
    expect(inputFormFor({ type: "object" })).toEqual(json);
    expect(inputFormFor({ type: "object", properties: { tags: { type: "array" } } })).toEqual(json);
    expect(inputFormFor({ type: "object", properties: { when: { type: ["string", "null"] } } })).toEqual(json);
    expect(inputFormFor({ type: "object", properties: { a: { type: "string" } }, oneOf: [] })).toEqual(json);
    expect(inputFormFor({ type: "object", properties: { a: { $ref: "#/$defs/a" } } })).toEqual(json);
    expect(inputFormFor({ type: "object", properties: { a: { enum: ["x", 1] } } })).toEqual(json);
    expect(inputFormFor({ type: "object", properties: { a: { type: "string" } }, required: ["b"] })).toEqual(json);
  });

  it("names properties for people", () => {
    expect(humanize("max_results")).toBe("Max results");
    expect(humanize("dueDate")).toBe("Due date");
    expect(humanize("region-code")).toBe("Region code");
  });
});

describe("the input a form sends", () => {
  const fields = fieldsOf({
    type: "object",
    properties: {
      topic: { type: "string", minLength: 2, maxLength: 5 },
      tone: { type: "string", enum: ["formal", "casual"] },
      count: { type: "integer", minimum: 1, maximum: 10 },
      share: { type: "number", exclusiveMinimum: 0 },
      draft: { type: "boolean" },
      note: { type: "string" },
    },
    required: ["topic", "tone"],
  });

  it("sends the values typed, leaving out the empty optional ones", () => {
    expect(buildInput(fields, { topic: "Q4", tone: "0", count: " 4 ", share: "0.5", draft: false, note: "" })).toEqual({
      ok: true,
      input: { topic: "Q4", tone: "formal", count: 4, share: 0.5, draft: false },
    });
  });

  it("says what is wrong with each value", () => {
    expect(buildInput(fields, { topic: " ", tone: "", count: "2.5", share: "0", draft: true, note: "" })).toEqual({
      ok: false,
      errors: {
        topic: "Enter a value.",
        tone: "Choose a value.",
        count: "Enter a whole number.",
        share: `Enter more than ${(0).toLocaleString()}.`,
      },
    });
    const tooLong = buildInput(fields, { topic: "abcdef", tone: "1", count: "11", share: "x", draft: true });
    expect(tooLong).toEqual({
      ok: false,
      errors: {
        topic: `Use at most ${(5).toLocaleString()} characters.`,
        count: `Enter ${(10).toLocaleString()} or less.`,
        share: "Enter a number.",
      },
    });
    // Lengths count characters as JSON Schema does: an emoji is one.
    expect(buildInput(fields, { topic: "😀😀", tone: "1" })).toMatchObject({ ok: true, input: { topic: "😀😀" } });
    expect(buildInput(fields, { topic: "😀", tone: "1" })).toEqual({
      ok: false,
      errors: { topic: `Use at least ${(2).toLocaleString()} characters.` },
    });
  });
});

describe("a JSON object typed by the user", () => {
  it("is null when empty, the object when valid, and a problem otherwise", () => {
    expect(parseJsonObject("  ")).toEqual({ ok: true, value: null });
    expect(parseJsonObject('{"a": 1}')).toEqual({ ok: true, value: { a: 1 } });
    expect(parseJsonObject("{a: 1}")).toEqual({ ok: false, error: "This is not valid JSON." });
    expect(parseJsonObject("[]")).toEqual({ ok: false, error: "Enter a JSON object, in braces: { … }." });
    expect(parseJsonObject("null")).toEqual({ ok: false, error: "Enter a JSON object, in braces: { … }." });
  });
});
