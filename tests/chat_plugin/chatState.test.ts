import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  applyEvent,
  assistantText,
  describeArgs,
  finish,
  humanizeTool,
  MAX_STORED_MESSAGES,
  newAssistantMessage,
  restore,
  serialize,
  type AssistantMessage,
  type ChatMessage,
} from '../../plugins/kekaflow-chat-app/src/chatState.ts';

const ev = (event: string, data: unknown) => ({ event, data });
const replay = (events: Array<{ event: string; data: unknown }>, start: AssistantMessage = newAssistantMessage('a')) => events.reduce(applyEvent, start);

describe('applyEvent', () => {
  it('appends streamed text into one part', () => {
    const msg = replay([ev('text', { delta: 'Hel' }), ev('text', { delta: 'lo' })]);
    assert.deepEqual(msg.parts, [{ type: 'text', text: 'Hello' }]);
    assert.equal(assistantText(msg), 'Hello');
  });

  it('keeps text, tool call and more text in the order they happened', () => {
    const msg = replay([
      ev('text', { delta: 'Checking. ' }),
      ev('tool_call', { id: 'c1', name: 'summarize_metric', args: { promql: 'up' } }),
      ev('tool_result', { id: 'c1', name: 'summarize_metric', ok: true, preview: '{}' }),
      ev('text', { delta: 'Done.' }),
    ]);
    assert.deepEqual(
      msg.parts.map((p) => p.type),
      ['text', 'tool', 'text']
    );
    assert.equal(assistantText(msg), 'Checking. Done.');
  });

  it('shows a tool as running until its result arrives', () => {
    const running = replay([ev('tool_call', { id: 'c1', name: 'q', args: {} })]);
    const part = running.parts[0];
    assert.equal(part.type === 'tool' && part.call.status, 'running');
  });

  it('marks a result ok or failed', () => {
    const base = [ev('tool_call', { id: 'c1', name: 'q', args: {} }), ev('tool_call', { id: 'c2', name: 'r', args: {} })];
    const msg = replay([...base, ev('tool_result', { id: 'c1', ok: true, preview: 'fine' }), ev('tool_result', { id: 'c2', ok: false, preview: 'bad' })]);
    const calls = msg.parts.flatMap((p) => (p.type === 'tool' ? [p.call] : []));
    assert.deepEqual(
      calls.map((c) => [c.status, c.preview]),
      [
        ['ok', 'fine'],
        ['error', 'bad'],
      ]
    );
  });

  it('matches a result without an id to the oldest running call of that name', () => {
    const msg = replay([
      ev('tool_call', { id: 'c1', name: 'q', args: {} }),
      ev('tool_call', { id: 'c2', name: 'q', args: {} }),
      ev('tool_result', { name: 'q', ok: true, preview: 'first' }),
    ]);
    const calls = msg.parts.flatMap((p) => (p.type === 'tool' ? [p.call] : []));
    assert.deepEqual(
      calls.map((c) => c.status),
      ['ok', 'running']
    );
  });

  it('ignores a result for a call it never saw', () => {
    const before = replay([ev('text', { delta: 'x' })]);
    assert.equal(applyEvent(before, ev('tool_result', { id: 'ghost', name: 'g', ok: true })), before);
  });

  it('records an error without losing what was already written', () => {
    const msg = replay([ev('text', { delta: 'partial' }), ev('error', { message: 'The model provider rejected the API key.' })]);
    assert.equal(msg.error, 'The model provider rejected the API key.');
    assert.equal(assistantText(msg), 'partial');
  });

  it('finishes on done and records token usage', () => {
    const msg = replay([ev('text', { delta: 'x' }), ev('done', { input_tokens: 120, output_tokens: 30 })]);
    assert.equal(msg.pending, false);
    assert.deepEqual(msg.usage, { input: 120, output: 30 });
  });

  it('settles a tool that never reported back when the reply ends', () => {
    const msg = replay([ev('tool_call', { id: 'c1', name: 'q', args: {} }), ev('done', {})]);
    const part = msg.parts[0];
    assert.equal(part.type === 'tool' && part.call.status, 'error');
  });

  it('does not change the message it was given', () => {
    const start = newAssistantMessage('a');
    const frozen = JSON.stringify(start);
    replay([ev('text', { delta: 'x' }), ev('tool_call', { id: 'c', name: 'n', args: {} }), ev('done', {})], start);
    assert.equal(JSON.stringify(start), frozen);
  });

  it('ignores unknown events and malformed payloads', () => {
    const start = newAssistantMessage('a');
    assert.equal(applyEvent(start, ev('session', { session_id: 'x' })), start);
    assert.equal(applyEvent(start, ev('text', 'not an object')), start);
    assert.equal(applyEvent(start, ev('text', { delta: 42 })), start);
    assert.deepEqual(replay([ev('tool_call', 'oops')]).parts[0], { type: 'tool', call: { id: 'call-0', name: 'tool', args: {}, status: 'running' } });
  });
});

describe('finish', () => {
  it('ends a cut-off reply with a reason and settles running tools', () => {
    const msg = finish(replay([ev('tool_call', { id: 'c1', name: 'q', args: {} })]), 'Network lost');
    assert.equal(msg.pending, false);
    assert.equal(msg.error, 'Network lost');
    const part = msg.parts[0];
    assert.equal(part.type === 'tool' && part.call.status, 'error');
  });

  it('keeps an error the server already sent', () => {
    const msg = finish(replay([ev('error', { message: 'Server said' })]), 'Generic');
    assert.equal(msg.error, 'Server said');
  });

  it('shows no error when the user pressed Stop', () => {
    assert.equal(finish(replay([ev('text', { delta: 'x' })])).error, undefined);
  });
});

describe('persistence', () => {
  const sample = (): ChatMessage[] => [
    { id: 'u1', role: 'user', text: 'Is anything hot?' },
    replay(
      [ev('tool_call', { id: 'c1', name: 'summarize_metric', args: { promql: 'up' } }), ev('tool_result', { id: 'c1', ok: true, preview: 'ok' }), ev('text', { delta: 'No.' }), ev('done', { input_tokens: 1, output_tokens: 1 })],
      newAssistantMessage('a1')
    ),
  ];

  it('round-trips a conversation', () => {
    const restored = restore(serialize(sample()));
    assert.equal(restored.length, 2);
    assert.deepEqual(restored[0], sample()[0]);
    const reply = restored[1];
    assert.equal(reply.role === 'assistant' && assistantText(reply), 'No.');
  });

  it('stores a reply that was still streaming as interrupted', () => {
    const streaming = replay([ev('text', { delta: 'half an ans' })]);
    const restored = restore(serialize([streaming]))[0];
    assert.equal(restored.role === 'assistant' && restored.pending, false);
    assert.equal(restored.role === 'assistant' && restored.error, 'The reply was interrupted.');
  });

  it('never restores a pending message, whatever the stored data says', () => {
    const stored = JSON.stringify([{ id: 'a', role: 'assistant', parts: [], pending: true }]);
    const restored = restore(stored)[0];
    assert.equal(restored.role === 'assistant' && restored.pending, false);
  });

  it('keeps only the most recent messages', () => {
    const many: ChatMessage[] = Array.from({ length: MAX_STORED_MESSAGES + 25 }, (_, i) => ({ id: `u${i}`, role: 'user', text: String(i) }));
    const restored = restore(serialize(many));
    assert.equal(restored.length, MAX_STORED_MESSAGES);
    assert.equal(restored[restored.length - 1].id, `u${MAX_STORED_MESSAGES + 24}`);
  });

  it('returns an empty chat for missing or corrupt storage', () => {
    for (const bad of [null, '', 'not json', '{}', '"str"', '42', 'null']) {
      assert.deepEqual(restore(bad), [], String(bad));
    }
  });

  it('skips entries that are malformed and keeps the good ones', () => {
    const stored = JSON.stringify([null, 5, { id: 1 }, { id: 'x', role: 'robot' }, { id: 'u', role: 'user', text: 'kept' }, { id: 'a', role: 'assistant', parts: 'nope' }]);
    assert.deepEqual(restore(stored), [{ id: 'u', role: 'user', text: 'kept' }]);
  });

  it('restores running tools as failed, since they can no longer complete', () => {
    const stored = JSON.stringify([{ id: 'a', role: 'assistant', parts: [{ type: 'tool', call: { id: 'c', name: 'n', args: {}, status: 'running' } }] }]);
    const part = (restore(stored)[0] as AssistantMessage).parts[0];
    assert.equal(part.type === 'tool' && part.call.status, 'error');
  });
});

describe('labels', () => {
  it('humanizes tool names', () => {
    assert.equal(humanizeTool('detect_metric_anomalies'), 'Detect metric anomalies');
    assert.equal(humanizeTool('query_prometheus'), 'Query prometheus');
    assert.equal(humanizeTool(''), 'Tool');
  });

  it('summarises arguments by the most telling field', () => {
    assert.equal(describeArgs({ promql: 'avg by (region) (up)', lookback_minutes: 60 }), 'avg by (region) (up)');
    assert.equal(describeArgs({ logsql: 'level:error' }), 'level:error');
    assert.equal(describeArgs({ lookback_minutes: 5 }), '{"lookback_minutes":5}');
    assert.equal(describeArgs({}), '');
  });

  it('shortens long arguments', () => {
    const out = describeArgs({ promql: 'x'.repeat(200) }, 50);
    assert.equal(out.length, 50);
    assert.ok(out.endsWith('…'));
  });
});
