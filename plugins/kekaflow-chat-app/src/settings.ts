/**
 * The plugin's settings: which language model answers, and how Grafana reaches the agent.
 *
 * Pure functions with no imports, so Node can run them in the unit tests. The settings page
 * only renders these results.
 */

export type Provider = '' | 'anthropic' | 'litellm';

export const PROVIDERS: Array<{ value: Provider; label: string; description: string }> = [
  { value: '', label: 'Agent default', description: 'Use the model and API key that the assistant service was started with (CHAT_AGENT_MODEL and ANTHROPIC_API_KEY).' },
  { value: 'anthropic', label: 'Anthropic', description: 'Call Claude on the Anthropic API with the key below.' },
  { value: 'litellm', label: 'LiteLLM proxy', description: 'Send requests to your LiteLLM proxy, which routes them to the model you name.' },
];

/** What Grafana stores as the plugin's non-secret settings. */
export interface ChatJsonData {
  agentUrl?: string;
  llmProvider?: string;
  llmBaseUrl?: string;
  llmModel?: string;
  /** Grafana cannot tell an emptied secret from a saved one, so clearing the key is recorded here. */
  llmKeyCleared?: boolean;
  [key: string]: unknown;
}

export interface SettingsForm {
  agentUrl: string;
  provider: Provider;
  baseUrl: string;
  model: string;
}

/** Secrets the person typed in this visit. Empty means "leave as saved". */
export interface SecretInputs {
  agentToken: string;
  llmApiKey: string;
}

export interface Resets {
  agentToken: boolean;
  llmApiKey: boolean;
}

export type FormErrors = Partial<Record<'agentUrl' | 'baseUrl' | 'model' | 'agentToken', string>>;

export function asProvider(value: unknown): Provider {
  return value === 'anthropic' || value === 'litellm' ? value : '';
}

export function formFromJsonData(jsonData: ChatJsonData | undefined): SettingsForm {
  const data = jsonData ?? {};
  return {
    agentUrl: String(data.agentUrl ?? ''),
    provider: asProvider(data.llmProvider),
    baseUrl: String(data.llmBaseUrl ?? ''),
    model: String(data.llmModel ?? ''),
  };
}

/** Whether an API key is stored, taking a cleared key into account. */
export function keyConfigured(secureJsonFields: Record<string, boolean> | undefined, jsonData: ChatJsonData | undefined): boolean {
  return Boolean(secureJsonFields?.llmApiKey) && jsonData?.llmKeyCleared !== true;
}

function isHttpUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return (url.protocol === 'http:' || url.protocol === 'https:') && url.hostname !== '';
  } catch {
    return false;
  }
}

export function validate(form: SettingsForm, tokenConfigured: boolean, secrets: SecretInputs): FormErrors {
  const errors: FormErrors = {};
  const agentUrl = form.agentUrl.trim();
  if (!agentUrl) {
    errors.agentUrl = 'Enter the address of the assistant service.';
  } else if (!isHttpUrl(agentUrl)) {
    errors.agentUrl = 'The address must start with http:// or https://.';
  }
  if (!tokenConfigured && !secrets.agentToken.trim()) {
    errors.agentToken = 'Enter the token the assistant service expects (CHAT_AGENT_TOKEN).';
  }
  if (form.provider === 'litellm') {
    const baseUrl = form.baseUrl.trim();
    if (!baseUrl) {
      errors.baseUrl = 'Enter the address of your LiteLLM proxy.';
    } else if (!isHttpUrl(baseUrl)) {
      errors.baseUrl = 'The address must start with http:// or https://.';
    } else if (new URL(baseUrl).username || new URL(baseUrl).password) {
      errors.baseUrl = 'Put the key in the API key field, not in the address.';
    }
    if (!form.model.trim()) {
      errors.model = 'Enter the model name your proxy exposes.';
    }
  }
  return errors;
}

export interface SettingsPayload {
  enabled: boolean;
  pinned: boolean;
  jsonData: ChatJsonData;
  /** Left out when nothing changes, because Grafana keeps the stored secrets in that case. */
  secureJsonData?: Record<string, string>;
}

/**
 * The body for `POST /api/plugins/<id>/settings`.
 *
 * Every LLM key is always written, even when empty. Grafana's template engine renders a missing
 * key as "<no value>", and an explicit empty string is cleaner than relying on the agent to ignore that.
 */
export function buildPayload(
  form: SettingsForm,
  secrets: SecretInputs,
  resets: Resets,
  current: ChatJsonData | undefined,
  state: { enabled: boolean; pinned: boolean }
): SettingsPayload {
  const secureJsonData: Record<string, string> = {};
  const token = secrets.agentToken.trim();
  const key = secrets.llmApiKey.trim();
  if (token) {
    secureJsonData.agentToken = token;
  }
  let keyCleared = current?.llmKeyCleared === true;
  if (key) {
    secureJsonData.llmApiKey = key;
    keyCleared = false;
  } else if (resets.llmApiKey) {
    secureJsonData.llmApiKey = '';
    keyCleared = true;
  }

  const jsonData: ChatJsonData = {
    ...current,
    agentUrl: form.agentUrl.trim().replace(/\/+$/, ''),
    llmProvider: form.provider,
    llmBaseUrl: form.provider === 'litellm' ? form.baseUrl.trim() : '',
    llmModel: form.provider === '' ? '' : form.model.trim(),
    llmKeyCleared: keyCleared,
  };

  const payload: SettingsPayload = { enabled: state.enabled, pinned: state.pinned, jsonData };
  if (Object.keys(secureJsonData).length > 0) {
    payload.secureJsonData = secureJsonData;
  }
  return payload;
}

/** A short label for the chat page's header, without the routing prefix LiteLLM adds. */
export function modelLabel(model: string): string {
  return model.replace(/^(anthropic|openai)\//, '');
}

export interface LlmInfo {
  source?: string;
  provider?: string;
  model?: string;
  host?: string;
  error?: string;
}

/** Describes where answers come from, for the header tooltip. */
export function describeLlm(llm: LlmInfo | undefined): string {
  if (!llm) {
    return '';
  }
  if (llm.error) {
    return llm.error;
  }
  if (llm.source === 'environment') {
    return 'Model from the assistant service configuration.';
  }
  const via = llm.provider === 'litellm' ? `LiteLLM proxy${llm.host ? ` at ${llm.host}` : ''}` : 'Anthropic';
  return `Model from the assistant settings, through ${via}.`;
}
