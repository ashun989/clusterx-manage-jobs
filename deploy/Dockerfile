FROM node:22-bookworm AS web-build
WORKDIR /src/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --ignore-scripts
COPY web/ ./
RUN CLUSTERX_WEB_OUT_DIR=/tmp/web-dist npm run build

FROM python:3.12-slim
WORKDIR /app
COPY server/pyproject.toml ./server/pyproject.toml
COPY server/src ./server/src
COPY --from=web-build /tmp/web-dist /opt/clusterx-monitor/web
RUN pip install --no-cache-dir ./server
RUN useradd --system --create-home --home-dir /var/lib/clusterx-monitor clusterx-monitor
USER clusterx-monitor
EXPOSE 8765
ENV CLUSTERX_MONITOR_HOST=0.0.0.0
ENV CLUSTERX_MONITOR_PORT=8765
CMD ["clusterx-monitor", "serve", "--clusterx-config", "/etc/clusterx-monitor/clusterx.yaml", "--policy-config", "/etc/clusterx-monitor/resource-policy.json", "--group-config", "/etc/clusterx-monitor/groups.yaml", "--auth-config", "/etc/clusterx-monitor/admin.yaml", "--static-dir", "/opt/clusterx-monitor/web", "--host", "0.0.0.0", "--port", "8765"]
