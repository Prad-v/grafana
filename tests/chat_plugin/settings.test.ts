import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  asProvider,
  buildPayload,
  describeLlm,
  formFromJsonData,
  keyConfigured,
  modelLabel,
  validate,
  type ChatJsonData,
  type SettingsForm,
} from '../../plugins/kekaflow-chat-app/src/settings.ts';

const NONE = { agentToken: '', llmApiKey: '' };
const NO_RESETS = { agentToken: false, llmApiKey: false };
const STATE = { enabled: true, pinned: false };

const form = (patch: Partial<SettingsForm> = {}): SettingsForm => ({ agentUrl: 'http://chat-agent:8080', provider: '', baseUrl: '', model: '', ...patch });

describe('formFromJsonData', () => {
  it('reads saved settings', () => {
    assert.deepEqual(formFromJsonData({ agentUrl: 'http://a:1', llmProvider: 'litellm', llmBaseUrl: 'http://p:4000', llmModel: 'gpt-4o' }), {
      agentUrl: 'http://a:1',
      provider: 'litellm',
      baseUrl: 'http://p:4000',
      model: 'gpt-4o',
    });
  });

  it('starts empty and on the agent default when nothing is saved', () => {
    assert.deepEqual(formFromJsonData(undefined), { agentUrl: '', provider: '', baseUrl: '', model: '' });
    assert.deepEqual(formFromJsonData({}), { agentUrl: '', provider: '', baseUrl: '', model: '' });
  });

  it('treats an unknown provider as the agent default instead of showing a broken form', () => {
    assert.equal(asProvider('openai'), '');
    assert.equal(asProvider(42), '');
    assert.equal(formFromJsonData({ llmProvider: 'openai' }).provider, '');
  });
});

describe('keyConfigured', () => {
  it('is true when a key is stored', () => {
    assert.equal(keyConfigured({ llmApiKey: true }, {}), true);
  });

  it('is false when nothing is stored', () => {
    assert.equal(keyConfigured({}, {}), false);
    assert.equal(keyConfigured(undefined, undefined), false);
  });

  it('is false after the key was cleared, although Grafana still lists the field', () => {
    assert.equal(keyConfigured({ llmApiKey: true }, { llmKeyCleared: true }), false);
  });

  it('is true again once a new key replaced the cleared one', () => {
    assert.equal(keyConfigured({ llmApiKey: true }, { llmKeyCleared: false }), true);
  });
});

describe('validate', () => {
  it('accepts the agent default with a service address and a stored token', () => {
    assert.deepEqual(validate(form(), true, NONE), {});
  });

  it('requires the service address, and a http(s) one', () => {
    assert.ok(validate(form({ agentUrl: '' }), true, NONE).agentUrl);
    assert.ok(validate(form({ agentUrl: 'chat-agent:8080' }), true, NONE).agentUrl);
    assert.ok(validate(form({ agentUrl: 'ftp://chat-agent' }), true, NONE).agentUrl);
    assert.equal(validate(form({ agentUrl: 'https://chat-agent.example' }), true, NONE).agentUrl, undefined);
  });

  it('requires a token the first time, not on later saves', () => {
    assert.ok(validate(form(), false, NONE).agentToken);
    assert.equal(validate(form(), false, { agentToken: 'abc', llmApiKey: '' }).agentToken, undefined);
    assert.equal(validate(form(), true, NONE).agentToken, undefined);
  });

  it('needs nothing more for Anthropic: model and key fall back to the service', () => {
    assert.deepEqual(validate(form({ provider: 'anthropic' }), true, NONE), {});
  });

  it('needs a proxy address and a model for LiteLLM', () => {
    const errors = validate(form({ provider: 'litellm' }), true, NONE);
    assert.ok(errors.baseUrl);
    assert.ok(errors.model);
    assert.deepEqual(validate(form({ provider: 'litellm', baseUrl: 'http://litellm:4000', model: 'gpt-4o' }), true, NONE), {});
  });

  it('rejects a proxy address that is not a http(s) URL', () => {
    for (const bad of ['litellm:4000', 'file:///etc/passwd', 'http://', 'javascript:alert(1)']) {
      assert.ok(validate(form({ provider: 'litellm', baseUrl: bad, model: 'm' }), true, NONE).baseUrl, bad);
    }
  });

  it('rejects credentials in the proxy address', () => {
    assert.match(validate(form({ provider: 'litellm', baseUrl: 'http://user:pw@litellm:4000', model: 'm' }), true, NONE).baseUrl ?? '', /API key field/);
  });

  it('does not check proxy fields while another provider is chosen', () => {
    assert.deepEqual(validate(form({ provider: 'anthropic', baseUrl: 'garbage' }), true, NONE), {});
  });
});

describe('buildPayload', () => {
  it('writes every model setting, empty or not, so none renders as "<no value>"', () => {
    const { jsonData } = buildPayload(form(), NONE, NO_RESETS, undefined, STATE);
    assert.equal(jsonData.llmProvider, '');
    assert.equal(jsonData.llmBaseUrl, '');
    assert.equal(jsonData.llmModel, '');
    assert.equal(jsonData.agentUrl, 'http://chat-agent:8080');
  });

  it('saves LiteLLM settings trimmed, and the address without a trailing slash for the agent URL', () => {
    const { jsonData } = buildPayload(form({ agentUrl: ' http://chat-agent:8080/ ', provider: 'litellm', baseUrl: ' http://litellm:4000 ', model: ' gpt-4o ' }), NONE, NO_RESETS, undefined, STATE);
    assert.equal(jsonData.agentUrl, 'http://chat-agent:8080');
    assert.equal(jsonData.llmBaseUrl, 'http://litellm:4000');
    assert.equal(jsonData.llmModel, 'gpt-4o');
  });

  it('drops the proxy address when the provider is not LiteLLM, so a stale one is never sent', () => {
    const { jsonData } = buildPayload(form({ provider: 'anthropic', baseUrl: 'http://litellm:4000', model: 'claude-x' }), NONE, NO_RESETS, undefined, STATE);
    assert.equal(jsonData.llmBaseUrl, '');
    assert.equal(jsonData.llmModel, 'claude-x');
  });

  it('drops the model when going back to the agent default', () => {
    assert.equal(buildPayload(form({ provider: '', model: 'gpt-4o' }), NONE, NO_RESETS, undefined, STATE).jsonData.llmModel, '');
  });

  it('keeps settings it does not know about', () => {
    const { jsonData } = buildPayload(form(), NONE, NO_RESETS, { somethingElse: 'kept' } as ChatJsonData, STATE);
    assert.equal(jsonData.somethingElse, 'kept');
  });

  it('sends no secrets when none were typed, so Grafana keeps the stored ones', () => {
    assert.equal(buildPayload(form(), NONE, NO_RESETS, undefined, STATE).secureJsonData, undefined);
  });

  it('sends only the secret that was typed', () => {
    assert.deepEqual(buildPayload(form(), { agentToken: '', llmApiKey: ' sk-1 ' }, NO_RESETS, undefined, STATE).secureJsonData, { llmApiKey: 'sk-1' });
    assert.deepEqual(buildPayload(form(), { agentToken: 'tok', llmApiKey: '' }, NO_RESETS, undefined, STATE).secureJsonData, { agentToken: 'tok' });
  });

  it('clears the key with an empty string and records that it was cleared', () => {
    const payload = buildPayload(form(), NONE, { agentToken: false, llmApiKey: true }, undefined, STATE);
    assert.deepEqual(payload.secureJsonData, { llmApiKey: '' });
    assert.equal(payload.jsonData.llmKeyCleared, true);
  });

  it('a new key typed after a reset wins and un-records the clearing', () => {
    const payload = buildPayload(form(), { agentToken: '', llmApiKey: 'sk-new' }, { agentToken: false, llmApiKey: true }, { llmKeyCleared: true }, STATE);
    assert.deepEqual(payload.secureJsonData, { llmApiKey: 'sk-new' });
    assert.equal(payload.jsonData.llmKeyCleared, false);
  });

  it('keeps a recorded clearing across saves that do not touch the key', () => {
    assert.equal(buildPayload(form(), NONE, NO_RESETS, { llmKeyCleared: true }, STATE).jsonData.llmKeyCleared, true);
  });

  it('never puts a secret into jsonData', () => {
    const payload = buildPayload(form({ provider: 'litellm', baseUrl: 'http://p:1', model: 'm' }), { agentToken: 'TOKEN-SECRET', llmApiKey: 'KEY-SECRET' }, NO_RESETS, undefined, STATE);
    const stored = JSON.stringify(payload.jsonData);
    assert.ok(!stored.includes('TOKEN-SECRET') && !stored.includes('KEY-SECRET'));
  });

  it('carries over whether the plugin is enabled and pinned', () => {
    const payload = buildPayload(form(), NONE, NO_RESETS, undefined, { enabled: true, pinned: true });
    assert.equal(payload.enabled, true);
    assert.equal(payload.pinned, true);
  });
});

describe('labels', () => {
  it('drops the routing prefix from the model shown in the header', () => {
    assert.equal(modelLabel('anthropic/claude-sonnet-5-5'), 'claude-sonnet-5-5');
    assert.equal(modelLabel('openai/gpt-4o'), 'gpt-4o');
    assert.equal(modelLabel('gpt-fake'), 'gpt-fake');
    assert.equal(modelLabel('team/claude-alias'), 'team/claude-alias');
  });

  it('says where answers come from', () => {
    assert.match(describeLlm({ source: 'environment' }), /service configuration/);
    assert.match(describeLlm({ source: 'settings', provider: 'litellm', host: 'litellm:4000' }), /LiteLLM proxy at litellm:4000/);
    assert.match(describeLlm({ source: 'settings', provider: 'anthropic' }), /Anthropic/);
  });

  it('shows an error instead of a source when the settings are unusable', () => {
    assert.equal(describeLlm({ source: 'settings', error: 'The host is not allowed.' }), 'The host is not allowed.');
  });

  it('copes with an agent that does not report the model source yet', () => {
    assert.equal(describeLlm(undefined), '');
  });
});
