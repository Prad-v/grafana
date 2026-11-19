##@ Docker Compose
# Build, deploy and test with Docker Compose. Included from the main Makefile.

COMPOSE         ?= docker compose
COMPOSE_FILE_APP ?= docker-compose.yml
COMPOSE_FILE_TESTS ?= tests/docker-compose.yml
COMPOSE_APP     = $(COMPOSE) -f $(COMPOSE_FILE_APP)
COMPOSE_TESTS   = $(COMPOSE) -f $(COMPOSE_FILE_TESTS)

# Keycloak SSO demo: Keycloak has its own compose file, Grafana gets an override.
COMPOSE_FILE_KEYCLOAK ?= docker-compose.keycloak.yml
COMPOSE_FILE_SSO      ?= docker-compose.sso.yml
COMPOSE_KEYCLOAK = $(COMPOSE) -f $(COMPOSE_FILE_KEYCLOAK)
COMPOSE_SSO      = $(COMPOSE) -f $(COMPOSE_FILE_APP) -f $(COMPOSE_FILE_SSO)
# The e2e service lives in tests/, so hand it the root .env explicitly.
COMPOSE_SSO_TEST = $(COMPOSE) --env-file .env -f $(COMPOSE_FILE_TESTS) --profile sso

# Observability: VictoriaMetrics, VictoriaLogs and the GPU simulator have their
# own compose file; Grafana gets a second override that adds mcp-grafana.
COMPOSE_FILE_OBS         ?= docker-compose.observability.yml
COMPOSE_FILE_OBS_GRAFANA ?= docker-compose.observability-grafana.yml
COMPOSE_FILE_CHAT        ?= docker-compose.chat.yml
COMPOSE_OBS      = $(COMPOSE) -f $(COMPOSE_FILE_OBS)
# The full stack: Grafana with Keycloak SSO, the Victoria data sources, mcp-grafana
# and the chat assistant (agent service + Grafana app plugin).
COMPOSE_STACK    = $(COMPOSE) -f $(COMPOSE_FILE_APP) -f $(COMPOSE_FILE_SSO) -f $(COMPOSE_FILE_OBS_GRAFANA) -f $(COMPOSE_FILE_CHAT)
COMPOSE_CHAT_TEST = $(COMPOSE) --env-file .env -f $(COMPOSE_FILE_TESTS) --profile chat
COMPOSE_OBS_TEST = $(COMPOSE) --env-file .env -f $(COMPOSE_FILE_TESTS) --profile obs

# envval KEY,DEFAULT: value of KEY in .env (read, never sourced), else DEFAULT.
envval = $(or $(shell sed -n 's/^$(1)=//p' .env 2>/dev/null | tail -n 1),$(2))

# Build metadata passed to the Dockerfile through docker-compose.yml.
export COMMIT_SHA        := $(shell git rev-parse HEAD 2>/dev/null)
export BUILD_BRANCH      := $(shell git rev-parse --abbrev-ref HEAD 2>/dev/null)
export SOURCE_DATE_EPOCH := $(shell git log -1 --format=%ct 2>/dev/null)

.PHONY: compose-env
compose-env: ## Create .env from .env.example if it does not exist.
	@test -f .env || { cp .env.example .env && echo "Created .env from .env.example"; }

.PHONY: compose-config
compose-config: compose-env ## Validate and print the resolved Compose configuration.
	$(COMPOSE_APP) config

.PHONY: compose-build
compose-build: compose-env ## Build the Grafana image (extra args: COMPOSE_BUILD_ARGS="--no-cache").
	$(COMPOSE_APP) build $(COMPOSE_BUILD_ARGS) grafana

.PHONY: compose-up
compose-up: compose-env ## Start Grafana in the background without rebuilding.
	$(COMPOSE_APP) up -d --wait

.PHONY: compose-deploy
compose-deploy: compose-build compose-up ## Build the image and (re)deploy Grafana, waiting until healthy.
	@$(COMPOSE_APP) ps

.PHONY: compose-down
compose-down: ## Stop and remove the containers (data volume is kept).
	$(COMPOSE_APP) down --remove-orphans

.PHONY: compose-restart
compose-restart: ## Restart the Grafana container.
	$(COMPOSE_APP) restart grafana

.PHONY: compose-logs
compose-logs: ## Follow Grafana logs.
	$(COMPOSE_APP) logs -f --tail=200 grafana

.PHONY: compose-ps
compose-ps: ## Show container status.
	$(COMPOSE_APP) ps

.PHONY: compose-test
compose-test: ## Run every unit test under tests/ in containers (Go, simulator, chat agent and plugin, Helm chart).
	$(COMPOSE_TESTS) run --rm test; rc=$$?; \
	for svc in sim-unit anomaly-engine-unit chat-agent-unit chat-plugin-unit helm-chart; do \
	  if [ $$rc -eq 0 ]; then $(COMPOSE_TESTS) run --rm $$svc; rc=$$?; fi; \
	done; \
	$(COMPOSE_TESTS) down --volumes; exit $$rc

.PHONY: compose-clean
compose-clean: ## Stop everything and DELETE the data volume and built image.
	$(COMPOSE_APP) down --volumes --remove-orphans --rmi local
	$(COMPOSE_TESTS) down --volumes --remove-orphans

##@ Keycloak SSO demo (two tenants, org_mapping, team sync)

.PHONY: sso-env
sso-env: compose-env ## Append the Keycloak settings from .env.sso.example to .env if missing.
	@grep -q '^KEYCLOAK_ADMIN_PASSWORD=' .env || { cat .env.sso.example >> .env && echo "Added Keycloak settings to .env"; }

.PHONY: sso-keycloak-up
sso-keycloak-up: sso-env ## Start Keycloak with the demo realm, waiting until healthy.
	$(COMPOSE_KEYCLOAK) up -d --wait

.PHONY: sso-up
sso-up: sso-keycloak-up ## Start Keycloak and Grafana wired together (reuses the built image).
	$(COMPOSE_SSO) up -d --wait grafana
	$(COMPOSE_SSO) --profile bootstrap run --rm grafana-bootstrap
	# Org names in org_mapping resolve at startup, so restart once the orgs exist.
	$(COMPOSE_SSO) restart grafana
	$(COMPOSE_SSO) up -d --wait grafana
	@$(COMPOSE_KEYCLOAK) ps
	@$(COMPOSE_SSO) ps

.PHONY: sso-deploy
sso-deploy: compose-build sso-up ## Build the Grafana image, then start Keycloak and Grafana wired together.

.PHONY: sso-test
sso-test: sso-env ## Log in as every demo user through Keycloak and check orgs and teams.
	$(COMPOSE_SSO_TEST) run --rm sso-e2e; rc=$$?; $(COMPOSE_SSO_TEST) down --remove-orphans; exit $$rc

.PHONY: sso-logs
sso-logs: ## Follow Keycloak and Grafana logs.
	$(COMPOSE_KEYCLOAK) logs -f --tail=100 keycloak & $(COMPOSE_SSO) logs -f --tail=100 grafana; kill %1

.PHONY: sso-down
sso-down: ## Stop Keycloak and Grafana (Grafana data volume is kept).
	$(COMPOSE_SSO) down --remove-orphans
	$(COMPOSE_KEYCLOAK) down --remove-orphans

##@ Observability: VictoriaMetrics, VictoriaLogs, GPU simulator, mcp-grafana

.PHONY: obs-env
obs-env: compose-env ## Append the observability settings from .env.observability.example to .env if missing.
	@grep -q '^GPU_SIM_PORT=' .env || { cat .env.observability.example >> .env && echo "Added observability settings to .env"; }

.PHONY: obs-build
obs-build: obs-env ## Build the GPU simulator image.
	$(COMPOSE_OBS) build $(COMPOSE_BUILD_ARGS) gpu-simulator

.PHONY: obs-up
obs-up: obs-build ## Start VictoriaMetrics, VictoriaLogs and the GPU simulator (no Grafana needed).
	$(COMPOSE_OBS) up -d --wait
	@$(COMPOSE_OBS) ps
	@echo "Simulator UI:    http://localhost:$(call envval,GPU_SIM_PORT,8090)"
	@echo "VictoriaMetrics: http://localhost:$(call envval,VICTORIAMETRICS_PORT,8428)/vmui"
	@echo "VictoriaLogs:    http://localhost:$(call envval,VICTORIALOGS_PORT,9428)/select/vmui"

.PHONY: obs-test
obs-test: obs-env ## Check metrics and logs flow end to end (needs `make obs-up`).
	$(COMPOSE_OBS_TEST) run --rm obs-e2e; rc=$$?; $(COMPOSE_OBS_TEST) down --remove-orphans; exit $$rc

.PHONY: obs-logs
obs-logs: ## Follow VictoriaMetrics, VictoriaLogs and simulator logs.
	$(COMPOSE_OBS) logs -f --tail=100

.PHONY: obs-down
obs-down: ## Stop the observability services (metrics, logs and simulator data are kept).
	$(COMPOSE_OBS) down --remove-orphans

.PHONY: obs-clean
obs-clean: ## Stop the observability services and DELETE their data and the simulator image.
	$(COMPOSE_OBS) down --volumes --remove-orphans --rmi local

.PHONY: sim-test
sim-test: ## Run the GPU simulator unit tests in a container.
	$(COMPOSE_TESTS) run --rm sim-unit; rc=$$?; $(COMPOSE_TESTS) down --volumes; exit $$rc

##@ Anomaly detection

.PHONY: anomaly-test
anomaly-test: ## Run the anomaly detection engine tests (race detector on) in a container.
	$(COMPOSE_TESTS) run --rm anomaly-engine-unit; rc=$$?; \
	$(COMPOSE_TESTS) down --volumes --remove-orphans; exit $$rc

##@ Chat assistant: Google ADK + Claude agent and Grafana app plugin

.PHONY: chat-env
chat-env: compose-env ## Add the chat settings to .env and generate CHAT_AGENT_TOKEN if missing.
	@grep -q '^CHAT_AGENT_PORT=' .env || { cat .env.chat.example >> .env && echo "Added chat settings to .env"; }
	@grep -q '^CHAT_AGENT_TOKEN=.' .env || { \
	  token=$$(openssl rand -hex 24) && \
	  sed -i.bak "s/^CHAT_AGENT_TOKEN=.*/CHAT_AGENT_TOKEN=$$token/" .env && rm -f .env.bak && \
	  echo "Generated CHAT_AGENT_TOKEN in .env"; }
	@grep -q '^ANTHROPIC_API_KEY=.' .env || echo "Note: set ANTHROPIC_API_KEY in .env, or the assistant cannot answer."

.PHONY: chat-build
chat-build: chat-env ## Build the agent and plugin images.
	$(COMPOSE_STACK) build $(COMPOSE_BUILD_ARGS) chat-agent chat-plugin

.PHONY: chat-test
chat-test: ## Run the agent, plugin and Helm chart tests in containers.
	$(COMPOSE_TESTS) run --rm chat-agent-unit; rc=$$?; \
	for svc in chat-plugin-unit helm-chart; do \
	  if [ $$rc -eq 0 ]; then $(COMPOSE_TESTS) run --rm $$svc; rc=$$?; fi; \
	done; \
	$(COMPOSE_TESTS) down --volumes --remove-orphans; exit $$rc

.PHONY: chat-e2e
chat-e2e: chat-env ## Check the running agent through Grafana (needs `make stack-up`).
	$(COMPOSE_CHAT_TEST) run --rm --use-aliases chat-e2e; rc=$$?; $(COMPOSE_CHAT_TEST) down --remove-orphans; exit $$rc

.PHONY: chat-logs
chat-logs: ## Follow the agent logs.
	$(COMPOSE_STACK) logs -f --tail=100 chat-agent

.PHONY: chat-restart
chat-restart: ## Recreate the agent, e.g. after changing the key or model in .env.
	$(COMPOSE_STACK) up -d --force-recreate --wait chat-agent

##@ Full stack: observability + Keycloak + Grafana + mcp-grafana + chat assistant

.PHONY: stack-up
stack-up: obs-env sso-env chat-env obs-up sso-keycloak-up ## Start everything: VictoriaMetrics, VictoriaLogs, simulator, Keycloak, Grafana, mcp-grafana, chat assistant (reuses the built image).
	$(COMPOSE_STACK) up -d --wait grafana
	$(COMPOSE_STACK) --profile bootstrap run --rm grafana-bootstrap
	# Org names in org_mapping resolve at startup, so restart once the orgs exist.
	$(COMPOSE_STACK) restart grafana
	$(COMPOSE_STACK) build chat-agent chat-plugin
	$(COMPOSE_STACK) up -d --wait grafana mcp-grafana chat-agent
	@$(MAKE) --no-print-directory stack-ps
	@echo
	@echo "Grafana:          http://localhost:$(call envval,GRAFANA_PORT,3000)   (dashboard: GPU fleet / GPU fleet overview)"
	@echo "Simulator UI:     http://localhost:$(call envval,GPU_SIM_PORT,8090)"
	@echo "Keycloak:         http://localhost:$(call envval,KEYCLOAK_PORT,8080)"
	@echo "mcp-grafana:      http://localhost:$(call envval,MCP_GRAFANA_PORT,8000)/mcp"
	@echo "Chat assistant:   http://localhost:$(call envval,GRAFANA_PORT,3000)/a/kekaflow-chat-app"

.PHONY: stack-deploy
stack-deploy: compose-build stack-up ## Build the Grafana image, then start the full stack.

.PHONY: stack-test
stack-test: obs-env ## Check the full stack end to end, including Grafana and mcp-grafana.
	$(COMPOSE_OBS_TEST) run --rm -e GRAFANA_INTERNAL_URL=http://grafana:3000 -e MCP_GRAFANA_URL=http://mcp-grafana:8000 obs-e2e; \
	rc=$$?; $(COMPOSE_OBS_TEST) down --remove-orphans; exit $$rc

.PHONY: stack-ps
stack-ps: ## Show the status of every container in the full stack.
	@$(COMPOSE_OBS) ps
	@$(COMPOSE_KEYCLOAK) ps
	@$(COMPOSE_STACK) ps

.PHONY: stack-logs
stack-logs: ## Follow logs of the whole stack.
	@trap 'kill 0' INT TERM; \
	$(COMPOSE_OBS) logs -f --tail=50 & \
	$(COMPOSE_KEYCLOAK) logs -f --tail=50 keycloak & \
	$(COMPOSE_STACK) logs -f --tail=50 grafana mcp-grafana chat-agent & \
	wait

.PHONY: stack-down
stack-down: ## Stop the whole stack (all data volumes are kept).
	$(COMPOSE_STACK) down --remove-orphans
	$(COMPOSE_KEYCLOAK) down --remove-orphans
	$(COMPOSE_OBS) down --remove-orphans
