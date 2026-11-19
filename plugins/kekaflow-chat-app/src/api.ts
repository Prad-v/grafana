import { config } from '@grafana/runtime';

import { PROXY_BASE } from './constants';
import { type LlmInfo } from './settings.ts';
import { SseParser, type SseEvent } from './sse.ts';

export interface AgentConfig {
  model: string;
  llm?: LlmInfo;
  mcp: { enabled: boolean; connected: boolean; tools: number; error?: string };
  native_tools: string[];
  allow_writes: boolean;
  limits: { max_message_chars: number; max_llm_calls: number };
}

function url(path: string): string {
  const base = config.appSubUrl.replace(/\/$/, '');
  return `${base}/${PROXY_BASE}/${path}`;
}

function headers(): Record<string, string> {
  return { 'Content-Type': 'application/json', 'X-Grafana-Org-Id': String(config.bootData.user.orgId) };
}

/** Turn a failed response into a message a person can act on. */
async function failure(resp: Response): Promise<Error> {
  let detail = '';
  try {
    const body: unknown = await resp.json();
    if (typeof body === 'object' && body !== null) {
      const record = body as Record<string, unknown>;
      detail = String(record.error ?? record.message ?? '');
    }
  } catch {
    // The body was not JSON; the status is all there is.
  }
  if (resp.status === 401 || resp.status === 403) {
    return new Error(detail || 'Grafana could not authenticate to the assistant service. Check CHAT_AGENT_TOKEN.');
  }
  if (resp.status === 404 || resp.status === 502 || resp.status === 503 || resp.status === 504) {
    return new Error('The assistant service is not reachable. Check that the chat-agent container is running.');
  }
  return new Error(detail || `The assistant service returned HTTP ${resp.status}.`);
}

export async function fetchConfig(signal?: AbortSignal): Promise<AgentConfig> {
  const resp = await fetch(url('config'), { headers: headers(), credentials: 'same-origin', signal });
  if (!resp.ok) {
    throw await failure(resp);
  }
  return (await resp.json()) as AgentConfig;
}

export async function resetSession(sessionId: string): Promise<void> {
  // Best effort: the conversation is already cleared in the browser.
  await fetch(url(`sessions/${encodeURIComponent(sessionId)}`), { method: 'DELETE', headers: headers(), credentials: 'same-origin' }).catch(() => undefined);
}

/**
 * Send a message and call `onEvent` for every event as it arrives.
 * Resolves when the stream ends. Rejects on a failed request or if `signal` aborts.
 */
export async function streamChat(message: string, sessionId: string, onEvent: (ev: SseEvent) => void, signal: AbortSignal): Promise<void> {
  const resp = await fetch(url('chat'), {
    method: 'POST',
    headers: headers(),
    credentials: 'same-origin',
    body: JSON.stringify({ message, session_id: sessionId }),
    signal,
  });
  if (!resp.ok) {
    throw await failure(resp);
  }
  if (!resp.body) {
    throw new Error('This browser cannot stream responses.');
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  const parser = new SseParser();
  for (;;) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    // `stream: true` keeps a multi-byte character that is split across chunks intact.
    parser.push(decoder.decode(value, { stream: true })).forEach(onEvent);
  }
  parser.push(decoder.decode()).forEach(onEvent);
  parser.flush().forEach(onEvent);
}

export interface LlmTestResult {
  ok: boolean;
  error?: string;
  latency_ms?: number;
  provider?: string;
  model?: string;
}

/** Make one tiny request to the model with the saved settings. A provider failure is a result, not a thrown error. */
export async function testLlm(): Promise<LlmTestResult> {
  const resp = await fetch(url('llm/test'), { method: 'POST', headers: headers(), credentials: 'same-origin' });
  if (!resp.ok) {
    throw await failure(resp);
  }
  return (await resp.json()) as LlmTestResult;
}

export interface ModelList {
  models: string[];
  error?: string;
}

/** The models the saved provider offers. Never throws for a provider problem: the page can still take a typed name. */
export async function fetchModels(): Promise<ModelList> {
  const resp = await fetch(url('llm/models'), { headers: headers(), credentials: 'same-origin' });
  if (!resp.ok) {
    throw await failure(resp);
  }
  return (await resp.json()) as ModelList;
}
