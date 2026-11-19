import { useEffect, useRef, useState } from 'react';

import { css } from '@emotion/css';

import { type GrafanaTheme2 } from '@grafana/data';
import { config, PluginPage } from '@grafana/runtime';
import { Alert, Badge, Button, Icon, LinkButton, useStyles2 } from '@grafana/ui';

import { fetchConfig, type AgentConfig } from '../api';
import { PLUGIN_ID, SUGGESTIONS } from '../constants';
import { describeLlm, modelLabel } from '../settings.ts';
import { useChat } from '../useChat';

import { Composer } from './Composer';
import { MessageView } from './MessageView';

type ConfigState = { status: 'loading' } | { status: 'ready'; config: AgentConfig } | { status: 'failed'; error: string };

function useAgentConfig(): ConfigState {
  const [state, setState] = useState<ConfigState>({ status: 'loading' });
  useEffect(() => {
    const controller = new AbortController();
    fetchConfig(controller.signal)
      .then((config) => setState({ status: 'ready', config }))
      .catch((err: unknown) => {
        if (!controller.signal.aborted) {
          setState({ status: 'failed', error: err instanceof Error ? err.message : 'The assistant service did not answer.' });
        }
      });
    return () => controller.abort();
  }, []);
  return state;
}

export function ChatPage() {
  const styles = useStyles2(getStyles);
  const { messages, busy, send, stop, reset } = useChat();
  const agent = useAgentConfig();
  const end = useRef<HTMLDivElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);

  // Follow the answer as it streams, unless the reader scrolled up to look at something.
  useEffect(() => {
    if (stickToBottom.current) {
      end.current?.scrollIntoView({ block: 'end' });
    }
  }, [messages]);

  const maxChars = agent.status === 'ready' ? agent.config.limits.max_message_chars : 4000;
  const unavailable = agent.status === 'failed';

  return (
    <PluginPage>
      <div className={styles.page}>
        <div className={styles.bar}>
          <div className={styles.status}>
            {agent.status === 'ready' && (
              <>
                <Badge
                  color={agent.config.llm?.error ? 'red' : 'green'}
                  text={agent.config.llm?.error ? 'Model settings invalid' : modelLabel(agent.config.model)}
                  icon="bolt"
                  tooltip={describeLlm(agent.config.llm)}
                />
                <Badge
                  color={agent.config.mcp.connected ? 'green' : 'orange'}
                  text={agent.config.mcp.connected ? `Grafana tools: ${agent.config.mcp.tools}` : 'Grafana tools offline'}
                  tooltip={agent.config.mcp.error}
                />
                <Badge color="blue" text={agent.config.allow_writes ? 'Can edit' : 'Read-only'} icon={agent.config.allow_writes ? 'pen' : 'eye'} />
              </>
            )}
          </div>
          <div className={styles.actions}>
            {config.bootData.user.orgRole === 'Admin' && (
              <LinkButton variant="secondary" size="sm" icon="cog" href={`${config.appSubUrl}/plugins/${PLUGIN_ID}?page=settings`}>
                Settings
              </LinkButton>
            )}
            <Button variant="secondary" size="sm" icon="plus" onClick={reset} disabled={messages.length === 0}>
              New chat
            </Button>
          </div>
        </div>

        {agent.status === 'ready' && agent.config.llm?.error && (
          <Alert severity="error" title="The assistant's model settings are not usable">
            {agent.config.llm.error}{' '}
            <a href={`${config.appSubUrl}/plugins/${PLUGIN_ID}?page=settings`}>Open the settings</a> (organization admins only).
          </Alert>
        )}

        {unavailable && (
          <Alert severity="error" title="The assistant is not available">
            {agent.error}
          </Alert>
        )}

        <div
          ref={list}
          className={styles.list}
          role="log"
          aria-live="polite"
          aria-label="Conversation"
          onScroll={(e) => {
            const el = e.currentTarget;
            stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
          }}
        >
          {messages.length === 0 ? (
            <div className={styles.welcome}>
              <Icon name="comments-alt" size="xxl" className={styles.welcomeIcon} />
              <h3>Ask about your observability data</h3>
              <p className={styles.muted}>
                Query metrics and logs, check alerts, read dashboards, and find unusual behaviour in plain language. Answers come from live data, and you can open any
                tool call to see the query that ran.
              </p>
              <div className={styles.suggestions}>
                {SUGGESTIONS.map((s) => (
                  <button key={s.title} type="button" className={styles.suggestion} disabled={unavailable || busy} onClick={() => void send(s.prompt)}>
                    <strong>{s.title}</strong>
                    <span>{s.prompt}</span>
                  </button>
                ))}
              </div>
            </div>
          ) : (
            messages.map((m) => <MessageView key={m.id} message={m} />)
          )}
          <div ref={end} />
        </div>

        <Composer
          busy={busy}
          maxChars={maxChars}
          onSend={(text) => {
            stickToBottom.current = true;
            void send(text);
          }}
          onStop={stop}
        />
        <div className={styles.footnote}>AI answers can be wrong. Check the query before you act on a result.</div>
      </div>
    </PluginPage>
  );
}

const getStyles = (theme: GrafanaTheme2) => ({
  page: css({ display: 'flex', flexDirection: 'column', gap: theme.spacing(1.5), height: 'calc(100vh - 230px)', minHeight: 420 }),
  bar: css({ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: theme.spacing(1), flexWrap: 'wrap' }),
  status: css({ display: 'flex', gap: theme.spacing(1), flexWrap: 'wrap' }),
  actions: css({ display: 'flex', gap: theme.spacing(1) }),
  list: css({
    flex: 1,
    overflowY: 'auto',
    padding: theme.spacing(2),
    border: `1px solid ${theme.colors.border.weak}`,
    borderRadius: theme.shape.radius.default,
    background: theme.colors.background.primary,
  }),
  welcome: css({ maxWidth: 820, margin: '0 auto', textAlign: 'center', paddingTop: theme.spacing(3) }),
  welcomeIcon: css({ color: theme.colors.primary.text }),
  muted: css({ color: theme.colors.text.secondary }),
  suggestions: css({
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))',
    gap: theme.spacing(1),
    marginTop: theme.spacing(2),
    textAlign: 'left',
  }),
  suggestion: css({
    display: 'flex',
    flexDirection: 'column',
    gap: theme.spacing(0.5),
    padding: theme.spacing(1.5),
    background: theme.colors.background.secondary,
    border: `1px solid ${theme.colors.border.weak}`,
    borderRadius: theme.shape.radius.default,
    color: theme.colors.text.primary,
    cursor: 'pointer',
    textAlign: 'left',
    '&:hover:not(:disabled)': { borderColor: theme.colors.primary.border, background: theme.colors.action.hover },
    '&:disabled': { opacity: 0.5, cursor: 'not-allowed' },
    span: { color: theme.colors.text.secondary, fontSize: theme.typography.bodySmall.fontSize },
  }),
  footnote: css({ color: theme.colors.text.disabled, fontSize: theme.typography.bodySmall.fontSize, textAlign: 'center' }),
});
