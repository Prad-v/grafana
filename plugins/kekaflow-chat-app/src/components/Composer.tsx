import { useState } from 'react';

import { css } from '@emotion/css';

import { type GrafanaTheme2 } from '@grafana/data';
import { Button, TextArea, useStyles2 } from '@grafana/ui';

interface Props {
  busy: boolean;
  maxChars: number;
  onSend: (text: string) => void;
  onStop: () => void;
}

export function Composer({ busy, maxChars, onSend, onStop }: Props) {
  const styles = useStyles2(getStyles);
  const [text, setText] = useState('');

  const submit = () => {
    if (text.trim() && !busy) {
      onSend(text);
      setText('');
    }
  };

  return (
    <div className={styles.wrap}>
      <TextArea
        className={styles.input}
        value={text}
        rows={2}
        maxLength={maxChars}
        placeholder="Ask about metrics, logs, alerts or anomalies. Enter to send, Shift+Enter for a new line."
        aria-label="Message"
        onChange={(e) => setText(e.currentTarget.value)}
        onKeyDown={(e) => {
          // isComposing: Enter confirms an IME candidate and must not send.
          if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            submit();
          }
        }}
      />
      {busy ? (
        <Button variant="secondary" icon="square-shape" onClick={onStop}>
          Stop
        </Button>
      ) : (
        <Button icon="message" onClick={submit} disabled={!text.trim()}>
          Send
        </Button>
      )}
    </div>
  );
}

const getStyles = (theme: GrafanaTheme2) => ({
  wrap: css({ display: 'flex', gap: theme.spacing(1), alignItems: 'flex-end' }),
  input: css({ flex: 1, resize: 'vertical', minHeight: 60 }),
});
