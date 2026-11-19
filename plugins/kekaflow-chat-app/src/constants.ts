export const PLUGIN_ID = 'kekaflow-chat-app';

/** Routes defined in plugin.json. Grafana's plugin proxy adds the agent token and forwards them. */
export const PROXY_BASE = `api/plugin-proxy/${PLUGIN_ID}/agent`;

export const SESSION_STORAGE_KEY = `${PLUGIN_ID}.conversation`;

/** Starting points that show what the assistant can do. Written for the GPU demo data. */
export const SUGGESTIONS: Array<{ title: string; prompt: string }> = [
  { title: 'Hottest GPUs', prompt: 'Which GPUs are running hottest right now? Group them by region.' },
  { title: 'Anything unusual?', prompt: 'Is there anything unusual in GPU temperature over the last 3 hours? Tell me when it started and where.' },
  { title: 'Firing alerts', prompt: 'Which alert rules are firing or pending right now?' },
  { title: 'Error logs', prompt: 'Did the number of error logs change in the last hour? Which nodes log the most errors?' },
  { title: 'Xid errors', prompt: 'Show me recent NVIDIA Xid errors and which GPUs they affect.' },
  { title: 'Explain a dashboard', prompt: 'Summarize the GPU fleet overview dashboard and what each panel shows.' },
];
