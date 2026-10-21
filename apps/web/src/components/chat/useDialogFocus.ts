/**
 * Focus handling of a modal panel (the conversation drawer, the document picker): focus moves
 * into it when it opens, stays inside while it is open, and returns to `returnFocusRef` (the
 * button that opened it) when it closes. Escape closes it.
 */

import { useEffect, useRef, type RefObject } from "react";

const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(", ");

export function useDialogFocus(
  panel: RefObject<HTMLElement | null>,
  returnFocusRef: RefObject<HTMLElement | null>,
  onClose: () => void,
): void {
  const close = useRef(onClose);

  useEffect(() => {
    close.current = onClose;
  });

  useEffect(() => {
    const element = panel.current;
    const opener = returnFocusRef.current;
    if (!element) return;
    const focusable = () => [...element.querySelectorAll<HTMLElement>(FOCUSABLE)];
    (focusable()[0] ?? element).focus();

    // On the document, not the panel: these keep working when the focused control goes away
    // (an archived or deleted conversation) and focus falls back to the page.
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      if (event.key === "Escape") {
        event.preventDefault();
        close.current();
        return;
      }
      if (event.key !== "Tab") return;
      // Wrap Tab and Shift+Tab around the panel's own controls.
      const controls = focusable();
      const first = controls[0];
      const last = controls.at(-1);
      if (!first || !last) return;
      const active = document.activeElement;
      if (!(active instanceof Node) || !element.contains(active)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      } else if (event.shiftKey && active === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    };
    // Anything that still moves focus behind the panel (a click, assistive technology) is
    // brought back into it.
    const onFocusIn = (event: FocusEvent) => {
      if (event.target instanceof Node && !element.contains(event.target)) {
        (focusable()[0] ?? element).focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("focusin", onFocusIn);
    // Runs after the closing render, once the page behind is no longer inert.
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("focusin", onFocusIn);
      opener?.focus();
    };
  }, [panel, returnFocusRef]);
}
