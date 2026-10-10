/** Whether a CSS media query matches, kept current as the viewport changes. */

import { useCallback, useSyncExternalStore } from "react";

/** The query's MediaQueryList, or null where matchMedia is missing (jsdom, very old browsers). */
function mediaQueryList(query: string): MediaQueryList | null {
  return typeof window.matchMedia === "function" ? window.matchMedia(query) : null;
}

export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback(
    (onChange: () => void) => {
      const list = mediaQueryList(query);
      list?.addEventListener("change", onChange);
      return () => {
        list?.removeEventListener("change", onChange);
      };
    },
    [query],
  );
  return useSyncExternalStore(
    subscribe,
    () => mediaQueryList(query)?.matches ?? false,
    () => false,
  );
}
