import { css } from '@emotion/css';
import { type AppPluginMeta, type GrafanaTheme2, type PluginConfigPageProps, type SelectableValue } from '@grafana/data';
import { getBackendSrv } from '@grafana/runtime';
import { Alert, Button, Field, FieldSet, Input, RadioButtonGroup, SecretInput, Select, useStyles2 } from '@grafana/ui';
import { useCallback, useEffect, useState } from 'react';

import { fetchConfig, fetchModels, testLlm } from '../api';
import { PLUGIN_ID } from '../constants';
import {
  buildPayload,
  describeLlm,
  formFromJsonData,
  keyConfigured,
  modelLabel,
  PROVIDERS,
  validate,
  type ChatJsonData,
  type FormErrors,
  type Provider,
} from '../settings.ts';

type Props = PluginConfigPageProps<AppPluginMeta<ChatJsonData>>;

type Outcome = { severity: 'success' | 'error' | 'warning'; title: string; body?: string };

export function ConfigPage({ plugin }: Props) {
  const styles = useStyles2(getStyles);
  const meta = plugin.meta;

  // What is saved right now. Updated after each save so a second save starts from it.
  const [saved, setSaved] = useState<ChatJsonData>(meta.jsonData ?? {});
  const [tokenConfigured, setTokenConfigured] = useState(Boolean(meta.secureJsonFields?.agentToken));
  const [keyIsConfigured, setKeyIsConfigured] = useState(keyConfigured(meta.secureJsonFields, meta.jsonData));

  const [form, setForm] = useState(() => formFromJsonData(meta.jsonData));
  const [apiKey, setApiKey] = useState('');
  const [agentToken, setAgentToken] = useState('');
  const [resetKey, setResetKey] = useState(false);
  const [resetToken, setResetToken] = useState(false);

  const [errors, setErrors] = useState<FormErrors>({});
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | undefined>();
  const [current, setCurrent] = useState<string>('');
  const [models, setModels] = useState<string[]>([]);

  const refresh = useCallback(async () => {
    try {
      const config = await fetchConfig();
      setCurrent(config.llm?.error ? `Not working: ${config.llm.error}` : `${modelLabel(config.model)}. ${describeLlm(config.llm)}`);
    } catch (err) {
      setCurrent(`The assistant service is not reachable. ${err instanceof Error ? err.message : ''}`.trim());
    }
    try {
      const list = await fetchModels();
      setModels(list.models);
    } catch {
      setModels([]);
    }
  }, []);

  useEffect(() => {
    if (meta.enabled) {
      void refresh();
    }
  }, [meta.enabled, refresh]);

  const update = (patch: Partial<typeof form>) => setForm((f) => ({ ...f, ...patch }));

  const save = async () => {
    const found = validate(form, tokenConfigured && !resetToken, { agentToken, llmApiKey: apiKey });
    setErrors(found);
    if (Object.keys(found).length > 0) {
      setOutcome({ severity: 'warning', title: 'Fix the highlighted fields first.' });
      return;
    }
    setBusy(true);
    setOutcome(undefined);
    const payload = buildPayload(form, { agentToken, llmApiKey: apiKey }, { agentToken: false, llmApiKey: resetKey }, saved, { enabled: Boolean(meta.enabled), pinned: Boolean(meta.pinned) });
    try {
      await getBackendSrv().post(`/api/plugins/${PLUGIN_ID}/settings`, payload);
    } catch (err) {
      setBusy(false);
      setOutcome({ severity: 'error', title: 'Grafana could not save the settings.', body: errorText(err) });
      return;
    }
    setSaved(payload.jsonData);
    if (agentToken.trim()) {
      setTokenConfigured(true);
      setResetToken(false);
    }
    setKeyIsConfigured(Boolean(apiKey.trim()) || (keyIsConfigured && !resetKey));
    setApiKey('');
    setAgentToken('');
    setResetKey(false);

    try {
      const result = await testLlm();
      setOutcome(
        result.ok
          ? { severity: 'success', title: 'Saved. The model answered.', body: `${modelLabel(result.model ?? '')} replied in ${result.latency_ms ?? '?'} ms.` }
          : { severity: 'warning', title: 'Saved, but the model did not answer.', body: result.error }
      );
    } catch (err) {
      setOutcome({ severity: 'warning', title: 'Saved, but the assistant service could not be reached to test the model.', body: errorText(err) });
    }
    await refresh();
    setBusy(false);
  };

  const modelOptions: Array<SelectableValue<string>> = models.map((m) => ({ label: m, value: m }));
  const provider = PROVIDERS.find((p) => p.value === form.provider);

  return (
    <div className={styles.page}>
      {!meta.enabled && (
        <Alert severity="info" title="The plugin is not enabled">
          Enable it first, then come back to choose a model.
        </Alert>
      )}

      <FieldSet label="Language model">
        <p className={styles.help}>Choose which model answers questions. The choice applies to everyone in this organization.</p>
        <Field label="Provider">
          <RadioButtonGroup<Provider> options={PROVIDERS.map((p) => ({ label: p.label, value: p.value }))} value={form.provider} onChange={(value) => update({ provider: value })} />
        </Field>
        {provider && <p className={styles.help}>{provider.description}</p>}

        {form.provider === 'litellm' && (
          <Field label="LiteLLM proxy address" description="Where the assistant service reaches the proxy, for example http://litellm:4000." invalid={Boolean(errors.baseUrl)} error={errors.baseUrl} required>
            <Input
              width={50}
              value={form.baseUrl}
              placeholder="http://litellm:4000"
              onChange={(e) => update({ baseUrl: e.currentTarget.value })}
              data-testid="llm-base-url"
            />
          </Field>
        )}

        {form.provider !== '' && (
          <>
            <Field
              label="Model"
              description={form.provider === 'litellm' ? 'The model name your proxy exposes.' : 'Leave empty to use the assistant service default.'}
              invalid={Boolean(errors.model)}
              error={errors.model}
              required={form.provider === 'litellm'}
            >
              <Select<string>
                width={50}
                options={modelOptions}
                value={form.model || null}
                allowCustomValue
                isClearable
                placeholder={form.provider === 'litellm' ? 'gpt-4o' : 'claude-sonnet-5-5'}
                noOptionsMessage="Save to load the models this provider offers, or type a name."
                onChange={(v) => update({ model: v?.value ?? '' })}
                onCreateOption={(v) => update({ model: v })}
                data-testid="llm-model"
              />
            </Field>
            <Field
              label="API key"
              description={
                form.provider === 'litellm'
                  ? 'The key your proxy expects. Leave empty if it needs none.'
                  : 'Stored encrypted in Grafana. Leave empty to use ANTHROPIC_API_KEY of the assistant service.'
              }
            >
              <SecretInput
                width={50}
                isConfigured={keyIsConfigured && !resetKey}
                value={apiKey}
                placeholder={form.provider === 'litellm' ? 'sk-...' : 'sk-ant-...'}
                onChange={(e) => setApiKey(e.currentTarget.value)}
                onReset={() => {
                  setResetKey(true);
                  setKeyIsConfigured(false);
                  setApiKey('');
                }}
                data-testid="llm-api-key"
              />
            </Field>
          </>
        )}
      </FieldSet>

      <FieldSet label="Assistant service">
        <p className={styles.help}>The Grafana plugin proxy sends requests here and adds the token, so the browser never sees it.</p>
        <Field label="Address" invalid={Boolean(errors.agentUrl)} error={errors.agentUrl} required>
          <Input width={50} value={form.agentUrl} placeholder="http://chat-agent:8080" onChange={(e) => update({ agentUrl: e.currentTarget.value })} data-testid="agent-url" />
        </Field>
        <Field label="Token" description="The same value as CHAT_AGENT_TOKEN on the service." invalid={Boolean(errors.agentToken)} error={errors.agentToken} required>
          <SecretInput
            width={50}
            isConfigured={tokenConfigured && !resetToken}
            value={agentToken}
            onChange={(e) => setAgentToken(e.currentTarget.value)}
            onReset={() => {
              setResetToken(true);
              setAgentToken('');
            }}
            data-testid="agent-token"
          />
        </Field>
      </FieldSet>

      {outcome && (
        <Alert severity={outcome.severity} title={outcome.title} data-testid="save-outcome">
          {outcome.body}
        </Alert>
      )}

      <div className={styles.actions}>
        <Button onClick={() => void save()} disabled={busy} icon={busy ? 'spinner' : 'check'} data-testid="save-and-test">
          Save and test
        </Button>
        {current && (
          <span className={styles.current} data-testid="current-model">
            Answering now: {current}
          </span>
        )}
      </div>
    </div>
  );
}

function errorText(err: unknown): string {
  if (typeof err === 'object' && err !== null) {
    const data = (err as { data?: { message?: string } }).data;
    if (data?.message) {
      return data.message;
    }
    if (err instanceof Error) {
      return err.message;
    }
  }
  return 'Unknown error.';
}

const getStyles = (theme: GrafanaTheme2) => ({
  page: css({ maxWidth: theme.spacing(100) }),
  help: css({ color: theme.colors.text.secondary, margin: theme.spacing(0, 0, 2) }),
  actions: css({ display: 'flex', alignItems: 'center', gap: theme.spacing(2), flexWrap: 'wrap' }),
  current: css({ color: theme.colors.text.secondary }),
});
