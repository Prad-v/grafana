import { useState } from 'react';

import { css } from '@emotion/css';

import { type GrafanaTheme2 } from '@grafana/data';
import { Icon, Spinner, useStyles2 } from '@grafana/ui';

import { describeArgs, humanizeTool, type ToolCall } from '../chatState.ts';

export function ToolCallRow({ call }: { call: ToolCall }) {
  const styles = useStyles2(getStyles);
  const [open, setOpen] = useState(false);
  const summary = describeArgs(call.args);

  return (
    <div className={styles.row} data-status={call.status}>
      <button type="button" className={styles.header} onClick={() => setOpen(!open)} aria-expanded={open}>
        <Icon name={open ? 'angle-down' : 'angle-right'} />
        {call.status === 'running' ? (
          <Spinner inline size="sm" />
        ) : call.status === 'ok' ? (
          <Icon name="check" className={styles.ok} />
        ) : (
          <Icon name="exclamation-triangle" className={styles.fail} />
        )}
        <span className={styles.name}>{humanizeTool(call.name)}</span>
        {summary && <code className={styles.summary}>{summary}</code>}
      </button>
      {open && (
        <div className={styles.body}>
          <div className={styles.label}>Arguments</div>
          <pre className={styles.pre}>{JSON.stringify(call.args, null, 2)}</pre>
          {call.preview && (
            <>
              <div className={styles.label}>Result</div>
              <pre className={styles.pre}>{call.preview}</pre>
            </>
          )}
        </div>
      )}
    </div>
  );
}

const getStyles = (theme: GrafanaTheme2) => ({
  row: css({
    border: `1px solid ${theme.colors.border.weak}`,
    borderRadius: theme.shape.radius.default,
    background: theme.colors.background.secondary,
    margin: theme.spacing(0.5, 0),
    fontSize: theme.typography.bodySmall.fontSize,
  }),
  header: css({
    display: 'flex',
    alignItems: 'center',
    gap: theme.spacing(1),
    width: '100%',
    padding: theme.spacing(0.5, 1),
    background: 'none',
    border: 'none',
    color: theme.colors.text.secondary,
    cursor: 'pointer',
    textAlign: 'left',
    minWidth: 0,
  }),
  name: css({ color: theme.colors.text.primary, whiteSpace: 'nowrap' }),
  summary: css({
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
    color: theme.colors.text.secondary,
    background: 'none',
    padding: 0,
    minWidth: 0,
  }),
  ok: css({ color: theme.colors.success.text }),
  fail: css({ color: theme.colors.error.text }),
  body: css({ padding: theme.spacing(0, 1, 1, 1) }),
  label: css({ color: theme.colors.text.secondary, margin: theme.spacing(1, 0, 0.5) }),
  pre: css({
    margin: 0,
    maxHeight: 240,
    overflow: 'auto',
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
    fontSize: theme.typography.bodySmall.fontSize,
  }),
});
