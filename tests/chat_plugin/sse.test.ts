import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { SseParser } from '../../plugins/kekaflow-chat-app/src/sse.ts';

const frame = (event: string, data: unknown) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;

describe('SseParser', () => {
  it('parses a whole event', () => {
    const events = new SseParser().push(frame('text', { delta: 'Hello' }));
    assert.deepEqual(events, [{ event: 'text', data: { delta: 'Hello' } }]);
  });

  it('parses several events in one chunk, in order', () => {
    const chunk = frame('session', { session_id: 'a' }) + frame('text', { delta: '1' }) + frame('done', {});
    assert.deepEqual(
      new SseParser().push(chunk).map((e) => e.event),
      ['session', 'text', 'done']
    );
  });

  it('waits for the rest of an event split across chunks', () => {
    const parser = new SseParser();
    const whole = frame('text', { delta: 'split me' });
    assert.deepEqual(parser.push(whole.slice(0, 12)), []);
    assert.deepEqual(parser.push(whole.slice(12, -1)), []);
    assert.deepEqual(parser.push(whole.slice(-1)), [{ event: 'text', data: { delta: 'split me' } }]);
  });

  it('handles a chunk boundary between the two newlines that end an event', () => {
    const parser = new SseParser();
    assert.deepEqual(parser.push('event: done\ndata: {}\n'), []);
    assert.deepEqual(parser.push('\n'), [{ event: 'done', data: {} }]);
  });

  it('treats CRLF and CR line endings like LF', () => {
    const events = new SseParser().push('event: text\r\ndata: {"delta":"x"}\r\n\r\n');
    assert.deepEqual(events, [{ event: 'text', data: { delta: 'x' } }]);
  });

  it('keeps CRLF intact when it is split between chunks', () => {
    const parser = new SseParser();
    assert.deepEqual(parser.push('event: text\r\ndata: {"delta":"x"}\r\n\r'), []);
    assert.deepEqual(parser.push('\n'), [{ event: 'text', data: { delta: 'x' } }]);
  });

  it('ignores comments used as keep-alives', () => {
    const events = new SseParser().push(': ping\n\n' + frame('text', { delta: 'a' }));
    assert.deepEqual(events, [{ event: 'text', data: { delta: 'a' } }]);
  });

  it('joins multi-line data with newlines', () => {
    const events = new SseParser().push('event: note\ndata: line one\ndata: line two\n\n');
    assert.deepEqual(events, [{ event: 'note', data: 'line one\nline two' }]);
  });

  it('keeps non-JSON data as a string instead of failing', () => {
    assert.deepEqual(new SseParser().push('event: error\ndata: plain words\n\n'), [{ event: 'error', data: 'plain words' }]);
  });

  it('defaults the event name to "message"', () => {
    assert.deepEqual(new SseParser().push('data: {"a":1}\n\n'), [{ event: 'message', data: { a: 1 } }]);
  });

  it('removes only the single space after the colon', () => {
    assert.deepEqual(new SseParser().push('data:  two spaces\n\n'), [{ event: 'message', data: ' two spaces' }]);
    assert.deepEqual(new SseParser().push('data:none\n\n'), [{ event: 'message', data: 'none' }]);
  });

  it('keeps unicode that came in one piece', () => {
    const events = new SseParser().push(frame('text', { delta: '温度 は 70°C — ok ✓' }));
    assert.equal((events[0].data as { delta: string }).delta, '温度 は 70°C — ok ✓');
  });

  it('drops blocks that have no data', () => {
    assert.deepEqual(new SseParser().push('event: lonely\n\n'), []);
  });

  it('flush returns a final event that never got its blank line', () => {
    const parser = new SseParser();
    assert.deepEqual(parser.push('event: done\ndata: {}'), []);
    assert.deepEqual(parser.flush(), [{ event: 'done', data: {} }]);
    assert.deepEqual(parser.flush(), []);
  });

  it('does not invent an event boundary when CRLF is split inside a block', () => {
    const parser = new SseParser();
    assert.deepEqual(parser.push('event: text\r'), []);
    assert.deepEqual(parser.push('\ndata: {"delta":"x"}\r\n\r\n'), [{ event: 'text', data: { delta: 'x' } }]);
  });

  it('survives every possible two-way split of a CRLF stream', () => {
    const stream = (frame('tool_call', { id: 'c', name: 'n', args: {} }) + frame('text', { delta: 'ok' }) + frame('done', {})).replace(/\n/g, '\r\n');
    const expected = new SseParser().push(stream);
    assert.equal(expected.length, 3);
    for (let cut = 0; cut <= stream.length; cut++) {
      const parser = new SseParser();
      assert.deepEqual([...parser.push(stream.slice(0, cut)), ...parser.push(stream.slice(cut))], expected, `split at ${cut}`);
    }
  });

  it('survives every possible two-way split of a realistic stream', () => {
    const stream = frame('session', { session_id: 's' }) + frame('tool_call', { id: 'c', name: 'n', args: { q: 'a\nb' } }) + frame('text', { delta: 'ok' }) + frame('done', { input_tokens: 1, output_tokens: 2 });
    const expected = new SseParser().push(stream);
    assert.equal(expected.length, 4);
    for (let cut = 0; cut <= stream.length; cut++) {
      const parser = new SseParser();
      const got = [...parser.push(stream.slice(0, cut)), ...parser.push(stream.slice(cut))];
      assert.deepEqual(got, expected, `split at ${cut}`);
    }
  });
});
