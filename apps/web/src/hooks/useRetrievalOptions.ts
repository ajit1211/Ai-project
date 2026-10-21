/**
 * The ways of searching documents this deployment supports (`GET /ai/retrieval/`), for the
 * chat's search control. Loaded the first time `wanted` is true (when a question is first
 * answered from documents), then kept. Null until then, and while the request fails: the chat
 * then offers no choice, and its questions use the server's default.
 */

import { useEffect, useState } from "react";

import { getRetrievalOptions } from "../api/ai";
import type { RetrievalOptions } from "../types/chat";

export function useRetrievalOptions(wanted: boolean): RetrievalOptions | null {
  const [options, setOptions] = useState<RetrievalOptions | null>(null);
  const loaded = options !== null;

  useEffect(() => {
    if (!wanted || loaded) return;
    const controller = new AbortController();
    getRetrievalOptions({ signal: controller.signal })
      .then((found) => {
        if (!controller.signal.aborted) setOptions(found);
      })
      .catch(() => {
        // No choice is offered; asked again the next time documents are chosen.
      });
    return () => {
      controller.abort();
    };
  }, [wanted, loaded]);

  return options;
}
