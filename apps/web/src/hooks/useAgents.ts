/**
 * The agent library's state: the agents the caller can see, and deleting one. A deletion is
 * confirmed by the server's answer, then the agent leaves the list (no refetch).
 */

import { useCallback, useEffect, useState } from "react";

import { deleteAgent, listAgents } from "../api/agents";
import { describeAgentError } from "../components/agents/labels";
import type { Agent } from "../types/agents";

interface ListState {
  /** Which request produced this state (the reload count). */
  key: number;
  agents: Agent[];
  /** The list could not be loaded: there is none to show, which is not an empty one. */
  failed: boolean;
}

export interface AgentLibrary {
  agents: Agent[];
  loading: boolean;
  failed: boolean;
  error: string | null;
  dismissError: () => void;
  reload: () => void;
  /** Deletes an agent; resolves to whether it was deleted. */
  remove: (agent: Agent) => Promise<boolean>;
  /** The last action's outcome, for the live region. */
  notice: string | null;
}

export function useAgents(): AgentLibrary {
  const [state, setState] = useState<ListState | null>(null);
  const [reloads, setReloads] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    listAgents({}, { signal: controller.signal })
      .then((agents) => {
        if (controller.signal.aborted) return;
        setState({ key: reloads, agents, failed: false });
        setError(null);
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setState((current) => ({ key: reloads, agents: current?.agents ?? [], failed: true }));
        setError(describeAgentError(err));
      });
    return () => {
      controller.abort();
    };
  }, [reloads]);

  const reload = useCallback(() => {
    setReloads((count) => count + 1);
  }, []);

  const remove = async (agent: Agent): Promise<boolean> => {
    setError(null);
    try {
      await deleteAgent(agent.id);
    } catch (err) {
      setError(describeAgentError(err, "You cannot delete this agent."));
      return false;
    }
    setState((current) => current && { ...current, agents: current.agents.filter((item) => item.id !== agent.id) });
    setNotice(`${agent.name} deleted.`);
    return true;
  };

  return {
    agents: state?.agents ?? [],
    loading: state?.key !== reloads,
    failed: state?.key === reloads && state.failed,
    error,
    dismissError: () => {
      setError(null);
    },
    reload,
    remove,
    notice,
  };
}
