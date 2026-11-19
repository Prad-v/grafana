/**
 * The conversation model and the pure functions that update it.
 *
 * A reply is an ordered list of parts, so text, tool calls and more text appear in the order
 * they happened. Everything here is immutable and free of browser APIs, so Node can test it.
 */
import type { SseEvent } from './sse.ts';

export type ToolStatus = 'running' | 'ok' | 'error';

export interface ToolCall {
  id: string;
  name: string;
  args: Record<string, unknown>;
  status: ToolStatus;
  preview?: string;
}

export type Part = { type: 'text'; text: string } | { type: 'tool'; call: ToolCall };

export interface Usage {
  input: number;
  output: number;
}

export interface UserMessage {
  id: string;
  role: 'user';
  text: string;
}

export interface AssistantMessage {
  id: string;
  role: 'assistant';
  parts: Part[];
  pending: boolean;
  error?: string;
  usage?: Usage;
}

export type ChatMessage = UserMessage | AssistantMessage;

export function newAssistantMessage(id: string): AssistantMessage {
  return { id, role: 'assistant', parts: [], pending: true };
}

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);
const str = (v: unknown, fallback = ''): string => (typeof v === 'string' ? v : fallback);
const num = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? v : 0);

/** Apply one stream event to an assistant message. Unknown events change nothing. */
export function applyEvent(msg: AssistantMessage, ev: SseEvent): AssistantMessage {
  const data = isRecord(ev.data) ? ev.data : {};
  switch (ev.event) {
    case 'text': {
      const delta = str(data.delta);
      if (!delta) {
        return msg;
      }
      const last = msg.parts[msg.parts.length - 1];
      if (last?.type === 'text') {
        return { ...msg, parts: [...msg.parts.slice(0, -1), { type: 'text', text: last.text + delta }] };
      }
      return { ...msg, parts: [...msg.parts, { type: 'text', text: delta }] };
    }
    case 'tool_call': {
      const call: ToolCall = {
        id: str(data.id, `call-${msg.parts.length}`),
        name: str(data.name, 'tool'),
        args: isRecord(data.args) ? data.args : {},
        status: 'running',
      };
      return { ...msg, parts: [...msg.parts, { type: 'tool', call }] };
    }
    case 'tool_result': {
      const id = str(data.id);
      const name = str(data.name);
      const status: ToolStatus = data.ok === false ? 'error' : 'ok';
      const preview = str(data.preview);
      let index = msg.parts.findIndex((p) => p.type === 'tool' && p.call.id === id);
      if (index === -1) {
        // The server did not echo an id: settle the oldest running call with that name.
        index = msg.parts.findIndex((p) => p.type === 'tool' && p.call.status === 'running' && p.call.name === name);
      }
      if (index === -1) {
        return msg;
      }
      const parts = msg.parts.slice();
      const part = parts[index];
      if (part.type === 'tool') {
        parts[index] = { type: 'tool', call: { ...part.call, status, preview } };
      }
      return { ...msg, parts };
    }
    case 'error':
      return { ...msg, error: str(data.message, 'The assistant failed.') };
    case 'done':
      return {
        ...msg,
        pending: false,
        usage: { input: num(data.input_tokens), output: num(data.output_tokens) },
        // A tool still "running" when the reply ends will never report back.
        parts: msg.parts.map((p) => (p.type === 'tool' && p.call.status === 'running' ? { type: 'tool', call: { ...p.call, status: 'error' } } : p)),
      };
    default:
      return msg;
  }
}

/** End a reply that was cut off, by the user pressing Stop or by a network failure. */
export function finish(msg: AssistantMessage, error?: string): AssistantMessage {
  return {
    ...msg,
    pending: false,
    error: msg.error ?? error,
    parts: msg.parts.map((p) => (p.type === 'tool' && p.call.status === 'running' ? { type: 'tool', call: { ...p.call, status: 'error' } } : p)),
  };
}

export function assistantText(msg: AssistantMessage): string {
  return msg.parts.map((p) => (p.type === 'text' ? p.text : '')).join('');
}

export const MAX_STORED_MESSAGES = 60;

/** Serialise for sessionStorage. A reply that was still streaming is stored as interrupted. */
export function serialize(messages: ChatMessage[]): string {
  const settled = messages.slice(-MAX_STORED_MESSAGES).map((m) => (m.role === 'assistant' && m.pending ? finish(m, 'The reply was interrupted.') : m));
  return JSON.stringify(settled);
}

/** Read stored messages back. Anything that does not look right is dropped, never thrown. */
export function restore(raw: string | null): ChatMessage[] {
  if (!raw) {
    return [];
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return [];
  }
  if (!Array.isArray(parsed)) {
    return [];
  }
  const out: ChatMessage[] = [];
  for (const item of parsed) {
    if (!isRecord(item) || typeof item.id !== 'string') {
      continue;
    }
    if (item.role === 'user' && typeof item.text === 'string') {
      out.push({ id: item.id, role: 'user', text: item.text });
    } else if (item.role === 'assistant' && Array.isArray(item.parts)) {
      const parts: Part[] = [];
      for (const p of item.parts) {
        if (!isRecord(p)) {
          continue;
        }
        if (p.type === 'text' && typeof p.text === 'string') {
          parts.push({ type: 'text', text: p.text });
        } else if (p.type === 'tool' && isRecord(p.call)) {
          const status: ToolStatus = p.call.status === 'ok' ? 'ok' : 'error';
          parts.push({
            type: 'tool',
            call: { id: str(p.call.id), name: str(p.call.name, 'tool'), args: isRecord(p.call.args) ? p.call.args : {}, status, preview: str(p.call.preview) || undefined },
          });
        }
      }
      out.push({ id: item.id, role: 'assistant', parts, pending: false, error: typeof item.error === 'string' ? item.error : undefined });
    }
  }
  return out;
}

/** "detect_metric_anomalies" becomes "Detect metric anomalies". */
export function humanizeTool(name: string): string {
  const words = name.replace(/_/g, ' ').trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : 'Tool';
}

/** A short, readable summary of a tool's arguments for the collapsed row. */
export function describeArgs(args: Record<string, unknown>, max = 90): string {
  const preferred = ['promql', 'logsql', 'query', 'expr', 'uid', 'datasourceUid', 'dashboardUid'];
  const key = preferred.find((k) => typeof args[k] === 'string' && args[k] !== '');
  const text = key ? String(args[key]) : Object.keys(args).length ? JSON.stringify(args) : '';
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}
