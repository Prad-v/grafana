/**
 * Incremental parser for a server-sent event stream.
 *
 * Network chunks do not respect event boundaries: one chunk can hold half an event, or
 * three. `push` buffers what is incomplete and returns only whole events.
 *
 * No imports, so Node can run it directly in the unit tests.
 */

export interface SseEvent {
  event: string;
  data: unknown;
}

export class SseParser {
  private buffer = '';
  // A chunk may end between the "\r" and "\n" of one CRLF; handling the "\r" alone would
  // count that line ending twice and invent an event boundary.
  private heldCr = false;

  push(chunk: string): SseEvent[] {
    let text = (this.heldCr ? '\r' : '') + chunk;
    this.heldCr = text.endsWith('\r');
    if (this.heldCr) {
      text = text.slice(0, -1);
    }
    // Normalise line endings so "\r\n\r\n" and "\n\n" are the same boundary.
    this.buffer += text.replace(/\r\n?/g, '\n');
    const events: SseEvent[] = [];
    let boundary = this.buffer.indexOf('\n\n');
    while (boundary !== -1) {
      const parsed = parseBlock(this.buffer.slice(0, boundary));
      if (parsed) {
        events.push(parsed);
      }
      this.buffer = this.buffer.slice(boundary + 2);
      boundary = this.buffer.indexOf('\n\n');
    }
    return events;
  }

  /** Whatever is left when the stream ends. A well-formed stream leaves nothing. */
  flush(): SseEvent[] {
    const rest = this.buffer.trim();
    this.buffer = '';
    this.heldCr = false;
    const parsed = rest ? parseBlock(rest) : undefined;
    return parsed ? [parsed] : [];
  }
}

function parseBlock(block: string): SseEvent | undefined {
  let event = 'message';
  const data: string[] = [];
  for (const line of block.split('\n')) {
    if (line === '' || line.startsWith(':')) {
      continue; // blank line or comment, which servers use as a keep-alive
    }
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    // One optional space after the colon belongs to the syntax, not to the value.
    const value = colon === -1 ? '' : line.slice(colon + 1).replace(/^ /, '');
    if (field === 'event') {
      event = value;
    } else if (field === 'data') {
      data.push(value);
    }
  }
  if (data.length === 0) {
    return undefined;
  }
  const raw = data.join('\n');
  try {
    return { event, data: JSON.parse(raw) };
  } catch {
    return { event, data: raw };
  }
}
