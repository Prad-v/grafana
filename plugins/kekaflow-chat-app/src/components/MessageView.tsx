import { css, cx } from '@emotion/css';

import { renderMarkdown, type GrafanaTheme2 } from '@grafana/data';
import { Alert, Spinner, useStyles2 } from '@grafana/ui';

import { type ChatMessage } from '../chatState.ts';

import { ToolCallRow } from './ToolCallRow';

export function MessageView({ message }: { message: ChatMessage }) {
  const styles = useStyles2(getStyles);

  if (message.role === 'user') {
    return (
      <div className={cx(styles.row, styles.userRow)}>
        <div className={styles.userBubble}>{message.text}</div>
      </div>
    );
  }

  const waiting = message.pending && message.parts.length === 0;
  return (
    <div className={styles.row}>
      <div className={styles.assistant}>
        {message.parts.map((part, i) =>
          part.type === 'text' ? (
            // renderMarkdown sanitises the HTML, which matters because the text can echo log lines.
            <div key={i} className={styles.markdown} dangerouslySetInnerHTML={{ __html: renderMarkdown(part.text) }} />
          ) : (
            <ToolCallRow key={part.call.id || i} call={part.call} />
          )
        )}
        {waiting && (
          <div className={styles.thinking}>
            <Spinner inline size="sm" /> Thinking…
          </div>
        )}
        {message.error && (
          <Alert severity="error" title="The assistant could not finish" className={styles.alert}>
            {message.error}
          </Alert>
        )}
        {!message.pending && message.usage && message.usage.output > 0 && (
          <div className={styles.usage}>
            {message.usage.input.toLocaleString()} tokens in, {message.usage.output.toLocaleString()} out
          </div>
        )}
      </div>
    </div>
  );
}

const getStyles = (theme: GrafanaTheme2) => ({
  row: css({ display: 'flex', marginBottom: theme.spacing(2) }),
  userRow: css({ justifyContent: 'flex-end' }),
  userBubble: css({
    maxWidth: '80%',
    padding: theme.spacing(1, 1.5),
    borderRadius: theme.shape.radius.default,
    background: theme.colors.primary.transparent,
    border: `1px solid ${theme.colors.primary.border}`,
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
  }),
  assistant: css({ maxWidth: '100%', minWidth: 0, flex: 1 }),
  thinking: css({ color: theme.colors.text.secondary, display: 'flex', alignItems: 'center', gap: theme.spacing(1) }),
  alert: css({ marginTop: theme.spacing(1), marginBottom: 0 }),
  usage: css({ color: theme.colors.text.disabled, fontSize: theme.typography.bodySmall.fontSize, marginTop: theme.spacing(0.5) }),
  markdown: css({
    wordBreak: 'break-word',
    'p, ul, ol, pre, table': { marginBottom: theme.spacing(1) },
    'p:last-child': { marginBottom: 0 },
    pre: {
      padding: theme.spacing(1),
      background: theme.colors.background.secondary,
      border: `1px solid ${theme.colors.border.weak}`,
      borderRadius: theme.shape.radius.default,
      overflow: 'auto',
    },
    code: { fontSize: theme.typography.bodySmall.fontSize },
    table: { borderCollapse: 'collapse', display: 'block', overflowX: 'auto', maxWidth: '100%' },
    'th, td': { border: `1px solid ${theme.colors.border.weak}`, padding: theme.spacing(0.5, 1), textAlign: 'left' },
    th: { background: theme.colors.background.secondary },
    a: { color: theme.colors.text.link },
  }),
});
