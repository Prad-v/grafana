import { useCallback, useEffect, useRef, useState } from 'react';

import { resetSession, streamChat } from './api';
import { applyEvent, finish, newAssistantMessage, restore, serialize, type AssistantMessage, type ChatMessage } from './chatState.ts';
import { SESSION_STORAGE_KEY } from './constants';

const SESSION_ID_KEY = `${SESSION_STORAGE_KEY}.session`;

// 32 hex characters: matches the agent's session id rules and is unguessable.
const newId = () => crypto.randomUUID().replace(/-/g, '');

function read(key: string): string | null {
  try {
    return window.sessionStorage.getItem(key);
  } catch {
    return null; // storage can be blocked; the chat still works, it just is not kept on reload
  }
}

function write(key: string, value: string) {
  try {
    window.sessionStorage.setItem(key, value);
  } catch {
    // Same as above, or the quota is full. Losing the saved copy is acceptable.
  }
}

export function useChat() {
  const [messages, setMessages] = useState<ChatMessage[]>(() => restore(read(SESSION_STORAGE_KEY)));
  const [sessionId, setSessionId] = useState<string>(() => read(SESSION_ID_KEY) ?? newId());
  const abort = useRef<AbortController | null>(null);
  const busy = messages.some((m) => m.role === 'assistant' && m.pending);

  // Keep the conversation across page reloads, but only once a reply has settled.
  useEffect(() => {
    if (!busy) {
      write(SESSION_STORAGE_KEY, serialize(messages));
      write(SESSION_ID_KEY, sessionId);
    }
  }, [messages, sessionId, busy]);

  useEffect(() => () => abort.current?.abort(), []);

  const send = useCallback(
    async (text: string) => {
      const prompt = text.trim();
      if (!prompt || abort.current) {
        return;
      }
      const stamp = Date.now();
      const assistantId = `a-${stamp}`;
      const controller = new AbortController();
      abort.current = controller;
      const update = (fn: (m: AssistantMessage) => AssistantMessage) =>
        setMessages((prev) => prev.map((m) => (m.id === assistantId && m.role === 'assistant' ? fn(m) : m)));

      setMessages((prev) => [...prev, { id: `u-${stamp}`, role: 'user', text: prompt }, newAssistantMessage(assistantId)]);
      try {
        await streamChat(prompt, sessionId, (ev) => update((m) => applyEvent(m, ev)), controller.signal);
        update((m) => (m.pending ? finish(m, 'The connection closed before the reply was complete.') : m));
      } catch (err) {
        const stopped = controller.signal.aborted;
        update((m) => finish(m, stopped ? undefined : err instanceof Error ? err.message : 'The request failed.'));
      } finally {
        abort.current = null;
      }
    },
    [sessionId]
  );

  const stop = useCallback(() => abort.current?.abort(), []);

  const reset = useCallback(() => {
    abort.current?.abort();
    abort.current = null;
    void resetSession(sessionId);
    setMessages([]);
    setSessionId(newId());
  }, [sessionId]);

  return { messages, busy, send, stop, reset };
}
