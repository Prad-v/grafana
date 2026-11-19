---
description: Learn how to sync OAuth, JWT, LDAP and auth proxy groups to Grafana teams in a multi-organization setup without Grafana Enterprise.
labels:
  products:
    - oss
title: Configure team sync for OAuth in Grafana OSS
weight: 610
---

# Configure team sync for OAuth in Grafana OSS

Team sync adds users to Grafana teams, and removes them, based on the groups your identity provider (IdP) asserts when they sign in. This article shows how to combine OAuth organization mapping with team sync so each tenant gets its own organization and teams.

This feature is separate from the Grafana Enterprise [team sync](../configure-team-sync/). It uses its own `[auth.team_sync]` settings and doesn't require a license.

Before you begin, ensure you have the following:

- An OAuth provider that returns a groups claim, configured with `groups_attribute_path`.
- One Grafana organization per tenant. Grafana doesn't create organizations from OAuth.
- Permission to edit `grafana.ini` or set environment variables.

## How it works

On every login, after Grafana syncs the user's organization roles, the team sync hook does the following for each organization that appears in `mappings`:

1. Skips the organization if the user isn't a member of it.
1. Finds the mapped teams by name, case-insensitively.
1. Adds the user to teams whose mapped group the IdP returned. The membership is flagged as external.
1. Removes the user from mapped teams when the IdP no longer returns the group, but only if the membership is external.

Grafana never touches:

- Memberships an administrator added manually.
- Teams that aren't referenced in `mappings`.
- Local password logins, which carry no IdP groups.

Team sync runs only at login. Sync errors are logged and never block the login.

## Configure multi-tenant OAuth with team sync

The following example maps IdP groups to organizations and roles with `org_mapping`, and to teams with `[auth.team_sync]`.

```ini
[auth.generic_oauth]
enabled = true
scopes = openid profile email groups
groups_attribute_path = groups
org_attribute_path = groups
org_mapping = tenant-a-admins:TenantA:Admin tenant-a-users:TenantA:Editor tenant-b-users:TenantB:Viewer

[auth.team_sync]
enabled = true
auto_create_teams = false
mappings = tenant-a-ops:TenantA:Ops tenant-a-dev:TenantA:Developers tenant-b-ops:TenantB:Ops
```

Groups claim values must match exactly, including case. Team names match case-insensitively.

## Settings reference

All settings live in the `[auth.team_sync]` section.

| Setting             | Environment variable                      | Default | Description                                                                    |
| ------------------- | ----------------------------------------- | ------- | ------------------------------------------------------------------------------ |
| `enabled`           | `GF_AUTH_TEAM_SYNC_ENABLED`               | `false` | Turns team sync on.                                                            |
| `mappings`          | `GF_AUTH_TEAM_SYNC_MAPPINGS`              | empty   | Space or comma separated `<EXTERNAL_GROUP>:<ORG_NAME_OR_ID>:<TEAM_NAME>` list. |
| `auto_create_teams` | `GF_AUTH_TEAM_SYNC_AUTO_CREATE_TEAMS`     | `false` | Creates a mapped team when it doesn't exist and the user has the group.        |

Escape a colon inside a value with `\:`. Quote values that contain spaces, for example `"Domain Admins:Main Org.:Admins"`. Use the JSON list syntax, `["a:1:Team A", "b:2:Team B"]`, if that's easier to read.

Team sync runs for identities authenticated by an OAuth provider, JWT, LDAP, auth proxy or SAML, and whose client requests team sync. It reads the groups the client extracted for the user. The unit tests cover the sync logic with OAuth, JWT, LDAP and auth proxy identities, but only OAuth has been designed and documented here, so verify the other providers in your environment before you rely on them.

## Run with Docker Compose

Use Docker Compose for local testing. The following snippet starts Grafana with team sync enabled against a Keycloak realm.

```yaml
services:
  grafana:
    image: grafana/grafana-oss:latest
    ports:
      - "3000:3000"
    environment:
      GF_AUTH_GENERIC_OAUTH_ENABLED: "true"
      GF_AUTH_GENERIC_OAUTH_CLIENT_ID: ${OAUTH_CLIENT_ID}
      GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET: ${OAUTH_CLIENT_SECRET}
      GF_AUTH_GENERIC_OAUTH_SCOPES: openid profile email groups
      GF_AUTH_GENERIC_OAUTH_GROUPS_ATTRIBUTE_PATH: groups
      GF_AUTH_GENERIC_OAUTH_ORG_ATTRIBUTE_PATH: groups
      GF_AUTH_GENERIC_OAUTH_ORG_MAPPING: tenant-a-users:TenantA:Editor
      GF_AUTH_TEAM_SYNC_ENABLED: "true"
      GF_AUTH_TEAM_SYNC_MAPPINGS: tenant-a-ops:TenantA:Ops
```

Put `OAUTH_CLIENT_ID` and `OAUTH_CLIENT_SECRET` in a `.env` file next to the compose file. Don't commit that file.

The official `grafana/grafana-oss` image doesn't contain this feature. Build an image from this repository to try it.

## Try it with Keycloak

The repository ships a Keycloak with two demo tenants, so you can see org mapping and team sync work end to end. Keycloak has its own Compose file, `docker-compose.keycloak.yml`. The override `docker-compose.sso.yml` points Grafana at it. Everything runs in containers.

```sh
make sso-deploy
```

The command builds the Grafana image, starts Keycloak with the `kekaflow` realm, starts Grafana with generic OAuth, `org_mapping` and `[auth.team_sync]`, creates the `TenantA` and `TenantB` organizations, and restarts Grafana so the org names in `org_mapping` resolve. Use `make sso-up` to skip the image build.

Open Grafana at `http://localhost:3000`, choose **Sign in with Keycloak**, and use one of these users. The password for all of them is `SSO_TEST_PASSWORD` in `.env`.

| User    | Keycloak groups                                      | Organizations and roles                        | Teams                 |
| ------- | ---------------------------------------------------- | ---------------------------------------------- | --------------------- |
| `alice` | `tenant-a-admins`, `tenant-a-ops`                    | TenantA: Admin                                 | TenantA: Ops          |
| `bob`   | `tenant-a-users`, `tenant-a-dev`                     | TenantA: Editor                                | TenantA: Developers   |
| `carol` | `tenant-b-users`, `tenant-b-ops`                     | TenantB: Viewer                                | TenantB: Ops          |
| `dave`  | `tenant-a-users`, `tenant-b-users`, `tenant-b-ops`   | TenantA: Editor, TenantB: Viewer               | TenantB: Ops          |
| `erin`  | `platform-admins`                                    | Main Org., TenantA, TenantB: Admin             | None                  |
| `frank` | None                                                 | Main Org.: Viewer (the default org)            | None                  |

Manage Keycloak at `http://localhost:8080` with `KEYCLOAK_ADMIN` and `KEYCLOAK_ADMIN_PASSWORD` from `.env`.

The demo uses its own Grafana data volume, so it doesn't touch the database of a plain `make compose-deploy`. Run `make sso-down` to stop it, and `make sso-logs` to follow the logs.

| Setting                   | Default                    | Description                                      |
| ------------------------- | -------------------------- | ------------------------------------------------ |
| `KEYCLOAK_TAG`            | `26.4.0`                   | Keycloak image tag.                              |
| `KEYCLOAK_PORT`           | `8080`                     | Host port for Keycloak.                          |
| `KEYCLOAK_ADMIN`          | `admin`                    | Keycloak master realm admin user.                |
| `KEYCLOAK_ADMIN_PASSWORD` | `admin`                    | Keycloak master realm admin password.            |
| `GRAFANA_CLIENT_SECRET`   | `grafana-local-client-secret` | Secret of the `grafana` OIDC client.          |
| `SSO_TEST_PASSWORD`       | `ChangeMe-sso-demo`        | Password of every demo user.                     |

`make sso-env` appends these settings to `.env` from `.env.sso.example` if they're missing. Change the defaults for anything beyond local use.

## Run the tests

The tests run in containers. They don't need Go or Python on your machine.

- **Unit tests:** `docker compose -f tests/docker-compose.yml run --rm test` builds the server packages to check the dependency injection wiring, runs `go vet`, and runs the Go tests in `tests/teamsync`.
- **End-to-end tests:** `make sso-test` logs in as every demo user through the real authorization code flow and reads the orgs, roles and teams back from the Grafana API. It also checks that tenants stay isolated, that logins are idempotent, that leaving a Keycloak group removes the team on the next login, and that manually added team members are kept. Run `make sso-up` first.

CI runs both on every commit.

## Troubleshooting

Use the following checks when users don't land in the expected teams.

- **Nothing happens:** Confirm `enabled = true` and that the log contains no `Skipping team sync mapping with invalid format` error. Each mapping needs exactly three parts.
- **User isn't added to a team:** Set `[log] filters = team.sync:debug`. Look for `Skipping team sync for organization the user is not a member of`. Fix `org_mapping` first, because team sync needs org membership.
- **Warning `Team referenced by team sync does not exist`:** Create the team, or set `auto_create_teams = true`.
- **Mappings are skipped with `could not resolve organization`:** The org name in `mappings` doesn't exist. Names are case-sensitive for organizations, so use the exact name or the numeric ID.
- **Warning `Could not fetch organization` for an `org_mapping` entry at startup:** Grafana resolves org names in `org_mapping` when it starts, so the organization must exist before that. Create the organizations, then restart Grafana. Numeric org IDs skip the lookup.
- **Groups are empty:** Check `groups_attribute_path` against the token or userinfo response. Team sync uses the same groups that Grafana extracts for the provider.
- **A removed group doesn't remove the user:** Only external memberships are removed. Remove manually added members in the team page.
- **A mapping was deleted from the configuration but the user stays in the team:** Grafana only manages teams that are still in `mappings`. Remove the stale members manually.

## Next steps

- [Configure generic OAuth authentication](../configure-authentication/generic-oauth/)
- [Configure Grafana Enterprise team sync](../configure-team-sync/)
- [Multi-team access](../multi-team-access/)
