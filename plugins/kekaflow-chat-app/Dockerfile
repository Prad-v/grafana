# Builds the Grafana app plugin, then ships only the compiled files.
#
# The final image is a one-shot installer: it copies the plugin into the volume that Grafana
# mounts at /var/lib/grafana/plugins/kekaflow-chat-app and exits (see docker-compose.chat.yml).
FROM node:22-slim AS build
WORKDIR /plugin
ENV npm_config_update_notifier=false npm_config_fund=false npm_config_audit=false
COPY package.json package-lock.json ./
RUN npm ci
COPY tsconfig.json webpack.config.js plugin.json ./
COPY src ./src
# Type errors fail the build: a plugin that compiles with errors is a plugin that breaks in the browser.
RUN npm run typecheck && npm run build

FROM busybox:1.37
COPY --from=build /plugin/dist /dist
CMD ["sh", "-c", "rm -rf /plugin/* && cp -a /dist/. /plugin/ && echo 'kekaflow-chat-app installed' && ls /plugin"]
