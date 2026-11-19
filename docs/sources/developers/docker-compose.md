---
title: Build and deploy with Docker Compose
menuTitle: Docker Compose
description: Build Grafana from source and run it locally with make and Docker Compose.
weight: 100
---

# Build and deploy with Docker Compose

This guide shows how to build Grafana from source and run it locally with `make` and Docker Compose. Everything runs in containers.

Before you begin, ensure you have the following:

- Docker with the Compose plugin
- `make`

You don't need Go or Node.js on the host.

## Quick start

Run the deploy target:

```sh
make compose-deploy
```

This creates `.env` from `.env.example` if it's missing, builds the image from the repository `Dockerfile` (`final-alpine` target), starts the container, and waits for the health check. Then open <http://localhost:3000> and sign in with the credentials from `.env` (`admin`/`admin` by default).

The first build compiles the frontend and backend and takes a long time. Later builds reuse the Docker layer cache.

## Make targets

Each target wraps a `docker compose` command. Run `make help` to list them.

| Target | Description |
| --- | --- |
| `compose-env` | Create `.env` from `.env.example` if it doesn't exist. |
| `compose-config` | Validate and print the resolved Compose configuration. |
| `compose-build` | Build the Grafana image. Pass `COMPOSE_BUILD_ARGS="--no-cache"` for a clean build. |
| `compose-up` | Start Grafana in the background without rebuilding. |
| `compose-deploy` | Build, then start, then wait until healthy. |
| `compose-down` | Stop and remove the containers. The data volume is kept. |
| `compose-restart` | Restart the Grafana container. |
| `compose-logs` | Follow Grafana logs. |
| `compose-ps` | Show container status. |
| `compose-test` | Run everything under `tests/` in a container. |
| `compose-clean` | Remove containers, the data volume, and the built image. This deletes Grafana data. |
| `sso-env` | Append the Keycloak settings from `.env.sso.example` to `.env` if they're missing. |
| `sso-deploy` | Build the Grafana image, then start Keycloak and Grafana wired together. |
| `sso-up` | Start Keycloak and Grafana wired together without rebuilding the image. |
| `sso-test` | Log in as every demo user through Keycloak and check orgs and teams. |
| `sso-logs` | Follow Keycloak and Grafana logs. |
| `sso-down` | Stop Keycloak and Grafana. The demo data volume is kept. |

| `obs-up` | Start VictoriaMetrics, VictoriaLogs, and the GPU simulator. |
| `stack-up` | Start the observability services, Keycloak, Grafana, and mcp-grafana together. |
| `sim-test`, `obs-test`, `stack-test` | Test the simulator and the observability stack. |

Refer to [Run the GPU observability stack with Docker Compose](../observability-stack/) for the full list of `obs-*` and `stack-*` targets.

The targets live in `docker.mk`, which the main `Makefile` includes. The services are defined in `docker-compose.yml`. The test runner is `tests/docker-compose.yml`. The Keycloak demo uses `docker-compose.keycloak.yml` and the Grafana override `docker-compose.sso.yml`. Refer to [Configure team sync for OAuth in Grafana OSS](../../setup-grafana/configure-access/configure-team-sync-oss/) for the demo users.

## Environment variables

Set these in `.env` to change the image, port, credentials, and build platform.

| Variable | Default | Description |
| --- | --- | --- |
| `GRAFANA_IMAGE` | `kekaflow/grafana` | Image name. |
| `GRAFANA_TAG` | `dev` | Image tag. |
| `GRAFANA_PORT` | `3000` | Host port. |
| `GF_SECURITY_ADMIN_USER` | `admin` | Initial admin user. |
| `GF_SECURITY_ADMIN_PASSWORD` | `admin` | Initial admin password. Change it for anything beyond local use. |
| `PLATFORM` | `linux/amd64` | Runtime platform of the container. |
| `JS_PLATFORM` | `linux/amd64` | Platform of the frontend build stage. |
| `JS_NODE_ENV` | `production` | Frontend build mode. |
| `GO_BUILD_TAGS` | `oss` | Go build tags. |
| `COMPOSE_PROJECT_NAME` | `kekaflow-grafana` | Compose project name. |

Any other `GF_<SECTION>_<KEY>` variable can be added to the `environment` block of `docker-compose.yml` to override a Grafana setting.

## Production

This Compose setup is for local build and test. Use Helm for production Kubernetes deployments.

## Troubleshooting

Use these fixes for common problems.

- **The build is very slow or runs out of memory on Apple Silicon.** The frontend builder is pinned to `linux/amd64` and runs under emulation. Give Docker Desktop at least 8 GB of memory.
- **`compose-up` times out waiting for healthy.** Run `make compose-logs`. The health check calls `/api/health` and allows about two minutes.
- **Port 3000 is already in use.** Set `GRAFANA_PORT` in `.env`.
- **Changed `.env` but nothing changed.** Run `make compose-up` again to recreate the container. Build arguments need `make compose-build`.
- **Reset to a clean state.** Run `make compose-clean`, then `make compose-deploy`.

## Next steps

- Refer to [Run the GPU observability stack with Docker Compose](../observability-stack/) to add VictoriaMetrics, VictoriaLogs, a mock GPU fleet, and mcp-grafana.
- Refer to `tests/docker-compose.yml` to see how the test suite runs in a container.
