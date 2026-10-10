/** The logical models this organization may use (`GET /ai/models/`), loaded once. */

import { useEffect, useState } from "react";

import { listModels } from "../api/ai";
import { describeError } from "../components/forms";
import type { ModelInfo } from "../types/ai";

export function useModels(): { models: ModelInfo[] | null; error: string | null } {
  const [models, setModels] = useState<ModelInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const cancelled = { current: false };
    listModels()
      .then((list) => {
        if (!cancelled.current) setModels(list);
      })
      .catch((err: unknown) => {
        if (!cancelled.current) setError(describeError(err));
      });
    return () => {
      cancelled.current = true;
    };
  }, []);

  return { models, error };
}
