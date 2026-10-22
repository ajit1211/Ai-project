/**
 * A labelled form control with a hint and an error under it, both tied to the control
 * (`aria-describedby`) so a screen reader reads them with it, and `aria-invalid` set while
 * there is an error. Unlike the shared `Field`, the label is a separate element, so the
 * control's name stays the label alone when an error appears.
 *
 * `children` renders the control from the props it must carry.
 */

import type { ReactNode } from "react";

export interface ControlProps {
  id: string;
  "aria-describedby": string | undefined;
  "aria-invalid": true | undefined;
  "aria-required": true | undefined;
}

export interface FormFieldProps {
  id: string;
  label: string;
  hint?: ReactNode;
  error?: string | undefined;
  required?: boolean;
  children: (control: ControlProps) => ReactNode;
}

/** The ids of a field's hint and error, for `aria-describedby`. */
export function describedBy(id: string, hint: boolean, error: boolean): string | undefined {
  const ids = [hint ? `${id}-hint` : null, error ? `${id}-error` : null].filter(Boolean);
  return ids.length > 0 ? ids.join(" ") : undefined;
}

export function FormField({ id, label, hint, error, required = false, children }: FormFieldProps) {
  const hasHint = hint !== undefined && hint !== null && hint !== "";
  return (
    <div className={`field${error ? " field-invalid" : ""}`}>
      <label htmlFor={id} className="field-label">
        {label}
        {required && (
          <span className="field-required" aria-hidden="true">
            {" "}
            *
          </span>
        )}
      </label>
      {children({
        id,
        "aria-describedby": describedBy(id, hasHint, Boolean(error)),
        "aria-invalid": error ? true : undefined,
        "aria-required": required ? true : undefined,
      })}
      {hasHint && (
        <span id={`${id}-hint`} className="field-hint">
          {hint}
        </span>
      )}
      {error && (
        <span id={`${id}-error`} className="field-error">
          {error}
        </span>
      )}
    </div>
  );
}
