/**
 * A task's input form, made from the agent's input schema (a JSON Schema). An object whose
 * properties are all simple (a string, possibly one of a list; a number or an integer; a
 * boolean) becomes one control per property, with the schema's titles, descriptions, required
 * list, bounds and defaults. Any other schema is edited as a JSON object. Either way the server
 * checks the input against the schema; the checks here only catch mistakes before sending.
 *
 * The schema is the agent author's text: titles and descriptions are shown as plain text.
 */

import type { JsonSchema } from "../../types/agents";

interface BaseField {
  /** The property's name: the key in `input`, and in the server's "input.<name>" errors. */
  name: string;
  label: string;
  description: string | null;
  required: boolean;
}

export interface TextField extends BaseField {
  kind: "text";
  minLength: number | null;
  maxLength: number | null;
  /** Long or unbounded text gets a text area. */
  multiline: boolean;
  initial: string;
}

/** One of a list (an `enum`); the control's value is the option's index, "" for none. */
export interface ChoiceField extends BaseField {
  kind: "choice";
  options: readonly (string | number)[];
  initial: string;
}

export interface NumberField extends BaseField {
  kind: "number";
  integer: boolean;
  minimum: number | null;
  maximum: number | null;
  exclusiveMinimum: number | null;
  exclusiveMaximum: number | null;
  initial: string;
}

export interface BooleanField extends BaseField {
  kind: "boolean";
  initial: boolean;
}

export type InputField = TextField | ChoiceField | NumberField | BooleanField;

export type InputForm =
  /** The agent takes a goal only. */
  | { kind: "none" }
  | { kind: "fields"; fields: InputField[] }
  /** Anything the fields cannot express: the whole input as a JSON object. */
  | { kind: "json" };

/** A control's value: the text of an input or a select, or whether a checkbox is ticked. */
export type InputValues = Record<string, string | boolean>;

/** Longer text than this (or text without a limit) is typed in a text area. */
const SINGLE_LINE_MAX = 200;

/** Keywords that combine, reference or constrain schemas in ways plain fields cannot show. */
const COMPLEX_KEYWORDS = [
  "$ref",
  "$dynamicRef",
  "allOf",
  "anyOf",
  "oneOf",
  "not",
  "if",
  "then",
  "else",
  "const",
  "dependentRequired",
  "dependentSchemas",
  "patternProperties",
  "propertyNames",
  "minProperties",
  "maxProperties",
  "unevaluatedProperties",
];

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const finite = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) ? value : null;

/** A count (a length limit): a whole number of at least zero. */
const count = (value: unknown): number | null =>
  typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;

const nonEmpty = (value: unknown): string | null =>
  typeof value === "string" && value.trim() !== "" ? value.trim() : null;

const isComplex = (schema: Record<string, unknown>): boolean =>
  COMPLEX_KEYWORDS.some((keyword) => Object.hasOwn(schema, keyword));

/** "max_results" → "Max results", "dueDate" → "Due date". */
export function humanize(name: string): string {
  const words = name
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/[_-]+/g, " ")
    .trim()
    .toLowerCase();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : name;
}

/** The field for one property, or null when it is not a simple one. */
function fieldFor(name: string, property: unknown, required: boolean): InputField | null {
  if (!isRecord(property) || isComplex(property)) return null;
  const base: BaseField = {
    name,
    label: nonEmpty(property.title) ?? humanize(name),
    description: nonEmpty(property.description),
    required,
  };
  const { type } = property;
  const fallback = property.default;

  if (Object.hasOwn(property, "enum")) {
    const options: unknown = property.enum;
    if (!Array.isArray(options) || options.length === 0) return null;
    const strings = options.every((option) => typeof option === "string");
    const numbers = options.every((option) => finite(option) !== null);
    if (!strings && !numbers) return null;
    const fits =
      type === undefined ||
      (type === "string" && strings) ||
      ((type === "number" || type === "integer") && numbers);
    if (!fits) return null;
    const listed = options as (string | number)[];
    const index = listed.findIndex((option) => option === fallback);
    return { ...base, kind: "choice", options: listed, initial: index === -1 ? "" : String(index) };
  }

  switch (type) {
    case "string": {
      const maxLength = count(property.maxLength);
      return {
        ...base,
        kind: "text",
        minLength: count(property.minLength),
        maxLength,
        multiline: maxLength === null || maxLength > SINGLE_LINE_MAX,
        initial: typeof fallback === "string" ? fallback : "",
      };
    }
    case "number":
    case "integer": {
      const initial = finite(fallback);
      return {
        ...base,
        kind: "number",
        integer: type === "integer",
        minimum: finite(property.minimum),
        maximum: finite(property.maximum),
        exclusiveMinimum: finite(property.exclusiveMinimum),
        exclusiveMaximum: finite(property.exclusiveMaximum),
        initial: initial === null ? "" : String(initial),
      };
    }
    case "boolean":
      return { ...base, kind: "boolean", initial: fallback === true };
    default:
      return null;
  }
}

/** How a task's input is asked for, given the agent's input schema. */
export function inputFormFor(schema: JsonSchema | null): InputForm {
  if (schema === null) return { kind: "none" };
  if (schema.type !== "object" || isComplex(schema)) return { kind: "json" };
  const properties = schema.properties ?? {};
  if (!isRecord(properties)) return { kind: "json" };
  const required: unknown[] = Array.isArray(schema.required) ? schema.required : [];
  if (!required.every((name) => typeof name === "string" && Object.hasOwn(properties, name))) {
    return { kind: "json" }; // a required value no property describes cannot be filled in
  }
  const names = Object.keys(properties);
  if (names.length === 0) {
    // Nothing is described: either nothing may be sent, or anything may.
    return schema.additionalProperties === false ? { kind: "none" } : { kind: "json" };
  }
  const fields: InputField[] = [];
  for (const name of names) {
    const field = fieldFor(name, properties[name], required.includes(name));
    if (!field) return { kind: "json" };
    fields.push(field);
  }
  return { kind: "fields", fields };
}

/** The controls' starting values: the schema's defaults, else empty. */
export function initialValues(fields: readonly InputField[]): InputValues {
  return Object.fromEntries(fields.map((field) => [field.name, field.initial]));
}

/** "Enter 3 or more.", with the bound written the user's way. */
const bound = (value: number): string => value.toLocaleString();

const SURROGATE_PAIR = /[\uD800-\uDBFF][\uDC00-\uDFFF]/g;

/** A text's length as JSON Schema counts it: in code points, so "😀" is one, not two. */
const codePoints = (text: string): number => text.length - (text.match(SURROGATE_PAIR)?.length ?? 0);

/** Why a number is not accepted, or null. */
function numberProblem(field: NumberField, value: number): string | null {
  if (field.integer && !Number.isInteger(value)) return "Enter a whole number.";
  if (field.minimum !== null && value < field.minimum) return `Enter ${bound(field.minimum)} or more.`;
  if (field.exclusiveMinimum !== null && value <= field.exclusiveMinimum) {
    return `Enter more than ${bound(field.exclusiveMinimum)}.`;
  }
  if (field.maximum !== null && value > field.maximum) return `Enter ${bound(field.maximum)} or less.`;
  if (field.exclusiveMaximum !== null && value >= field.exclusiveMaximum) {
    return `Enter less than ${bound(field.exclusiveMaximum)}.`;
  }
  return null;
}

export type InputResult =
  | { ok: true; input: Record<string, unknown> }
  | { ok: false; errors: Record<string, string> };

/**
 * The input to send, from the controls' values, or the problems by field name. Optional
 * values left empty are not sent; a checkbox always is (true or false).
 */
export function buildInput(fields: readonly InputField[], values: InputValues): InputResult {
  const input: Record<string, unknown> = {};
  const errors: Record<string, string> = {};
  for (const field of fields) {
    const raw = values[field.name];
    if (field.kind === "boolean") {
      input[field.name] = raw === true;
      continue;
    }
    const text = typeof raw === "string" ? raw : "";
    if (text.trim() === "") {
      if (field.required) errors[field.name] = field.kind === "choice" ? "Choose a value." : "Enter a value.";
      continue;
    }
    switch (field.kind) {
      case "text": {
        const length = codePoints(text);
        if (field.minLength !== null && length < field.minLength) {
          errors[field.name] = `Use at least ${bound(field.minLength)} characters.`;
        } else if (field.maxLength !== null && length > field.maxLength) {
          errors[field.name] = `Use at most ${bound(field.maxLength)} characters.`;
        } else {
          input[field.name] = text;
        }
        break;
      }
      case "choice": {
        const option = /^\d+$/.test(text) ? field.options[Number(text)] : undefined;
        if (option === undefined) errors[field.name] = "Choose a value.";
        else input[field.name] = option;
        break;
      }
      case "number": {
        const value = Number(text.trim());
        const problem = Number.isFinite(value) ? numberProblem(field, value) : "Enter a number.";
        if (problem) errors[field.name] = problem;
        else input[field.name] = value;
        break;
      }
    }
  }
  return Object.keys(errors).length > 0 ? { ok: false, errors } : { ok: true, input };
}

export type JsonObjectResult = { ok: true; value: Record<string, unknown> | null } | { ok: false; error: string };

/**
 * A JSON object typed by the user: null when the text is empty, an error when it is not
 * JSON or not an object.
 */
export function parseJsonObject(text: string): JsonObjectResult {
  if (text.trim() === "") return { ok: true, value: null };
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return { ok: false, error: "This is not valid JSON." };
  }
  if (!isRecord(value)) return { ok: false, error: "Enter a JSON object, in braces: { … }." };
  return { ok: true, value };
}
