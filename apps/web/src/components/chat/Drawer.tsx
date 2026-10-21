/**
 * A modal side drawer (the conversation list on narrow screens). Focus moves into it when it
 * opens, stays inside while it is open, and returns to `returnFocusRef` (the button that
 * opened it) when it closes. Escape and a click on the backdrop close it.
 */

import { useRef, type ReactNode, type RefObject } from "react";

import { useDialogFocus } from "./useDialogFocus";

export interface DrawerProps {
  label: string;
  onClose: () => void;
  returnFocusRef: RefObject<HTMLElement | null>;
  children: ReactNode;
}

export function Drawer({ label, onClose, returnFocusRef, children }: DrawerProps) {
  const panel = useRef<HTMLDivElement>(null);
  useDialogFocus(panel, returnFocusRef, onClose);

  return (
    <div className="chat-drawer-layer">
      <div className="chat-drawer-backdrop" onClick={onClose} />
      <div ref={panel} className="chat-drawer" role="dialog" aria-modal="true" aria-label={label} tabIndex={-1}>
        {children}
      </div>
    </div>
  );
}
