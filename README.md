![Grafana Logo (Light)](docs/logo-horizontal.png#gh-light-mode-only)
![Grafana Logo (Dark)](docs/logo-horizontal-dark.png#gh-dark-mode-only)

The open-source platform for monitoring and observability

[![License](https://img.shields.io/github/license/grafana/grafana)](LICENSE)

Grafana allows you to query, visualize, alert on and understand your metrics no matter where they are stored. Create, explore, and share dashboards with your team and foster a data-driven culture:

- **Visualizations:** Fast and flexible client side graphs with a multitude of options. Panel plugins offer many different ways to visualize metrics and logs.
- **Dynamic Dashboards:** Create dynamic & reusable dashboards with template variables that appear as dropdowns at the top of the dashboard.
- **Explore Metrics:** Explore your data through ad-hoc queries and dynamic drilldown. Split view and compare different time ranges, queries and data sources side by side.
- **Explore Logs:** Experience the magic of switching from metrics to logs with preserved label filters. Quickly search through all your logs or streaming them live.
- **Alerting:** Visually define alert rules for your most important metrics. Grafana will continuously evaluate and send notifications to systems like Slack, PagerDuty, VictorOps, OpsGenie.
- **Mixed Data Sources:** Mix different data sources in the same graph! You can specify a data source on a per-query basis. This works for even custom datasources.

## Get started

- [Get Grafana](https://grafana.com/get)
- [Installation guides](https://grafana.com/docs/grafana/latest/setup-grafana/installation/)

Unsure if Grafana is for you? Watch Grafana in action on [play.grafana.org](https://play.grafana.org/)!

## Documentation

The Grafana documentation is available at [grafana.com/docs](https://grafana.com/docs/).

## Local development stack

Build and run Grafana in containers, with no toolchain on the host:

- [Build and deploy with Docker Compose](docs/sources/developers/docker-compose.md): `make compose-deploy`.
- [Run the GPU observability stack](docs/sources/developers/observability-stack.md): `make stack-up` starts VictoriaMetrics, VictoriaLogs, a mock multi-region NVIDIA GPU fleet with a control UI, Keycloak, Grafana, and mcp-grafana.
- [Anomaly detection for any metrics data source](docs/sources/developers/anomaly-detection.md): the architecture and the Go detection engine (`backend/services/anomaly-engine`) behind a planned Grafana data source plugin that works with Mimir, Prometheus, and VictoriaMetrics. `make anomaly-test` runs its tests. See also the [user guide](docs/static/anomaly-detection/index.html).
- [Ask questions about your data with the chat assistant](docs/sources/developers/chat-assistant.md): a Grafana app plugin backed by a Google ADK and Claude agent that answers questions about metrics, logs, alerts, and anomalies through mcp-grafana. `make stack-up` starts it. See also the [user guide](docs/static/chat-assistant/index.html).

## Contributing

If you're interested in contributing to the Grafana project:

- Start by reading the [Contributing guide](https://github.com/grafana/grafana/blob/HEAD/CONTRIBUTING.md).
- Learn how to set up your local environment, in our [Developer guide](https://github.com/grafana/grafana/blob/HEAD/contribute/developer-guide.md).
- Explore our [beginner-friendly issues](https://github.com/grafana/grafana/issues?q=is%3Aopen+is%3Aissue+label%3A%22beginner+friendly%22).
- Look through our [style guide and Storybook](https://developers.grafana.com/ui/latest/index.html).

> Share your contributor experience in our [feedback survey](https://gra.fan/ome) to help us improve.

## Get involved

- Follow [@grafana on X (formerly Twitter)](https://x.com/grafana/).
- Read and subscribe to the [Grafana blog](https://grafana.com/blog/).
- If you have a specific question, check out our [discussion forums](https://community.grafana.com/).
- For general discussions, join us on the [official Slack](https://slack.grafana.com) team.

This project is tested with [BrowserStack](https://www.browserstack.com/).

## License

Grafana is distributed under [AGPL-3.0-only](LICENSE). For Apache-2.0 exceptions, see [LICENSING.md](https://github.com/grafana/grafana/blob/HEAD/LICENSING.md).
