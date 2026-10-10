/**
 * A modal side drawer (the conversation list on narrow screens). Focus moves into it when it
 * opens, stays inside while it is open, and returns to `returnFocusRef` (the button that
 * opened it) when it closes. Escape and a click on the backdrop close it.
 */

import { useEffect, useRef, type ReactNode, type RefObject } from "react";

const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(", ");

export interface DrawerProps {
  label: string;
  onClose: () => void;
  returnFocusRef: RefObject<HTMLElement | null>;
  children: ReactNode;
}

export function Drawer({ label, onClose, returnFocusRef, children }: DrawerProps) {
  const panel = useRef<HTMLDivElement>(null);
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
      // Wrap Tab and Shift+Tab around the drawer's own controls.
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
    // Anything that still moves focus behind the drawer (a click, assistive technology) is
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
  }, [returnFocusRef]);

  return (
    <div className="chat-drawer-layer">
      <div className="chat-drawer-backdrop" onClick={onClose} />
      <div ref={panel} className="chat-drawer" role="dialog" aria-modal="true" aria-label={label} tabIndex={-1}>
        {children}
      </div>
    </div>
  );
}
