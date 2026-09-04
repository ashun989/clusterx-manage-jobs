FROM node:22-bookworm AS web-build
WORKDIR /src/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --ignore-scripts
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY --from=web-build /src/src/clusterx_monitor/static ./src/clusterx_monitor/static
RUN pip install --no-cache-dir .
RUN useradd --system --create-home --home-dir /var/lib/clusterx-monitor clusterx-monitor
USER clusterx-monitor
EXPOSE 8765
ENV CLUSTERX_MONITOR_HOST=0.0.0.0
ENV CLUSTERX_MONITOR_PORT=8765
CMD ["clusterx-monitor", "serve", "--host", "0.0.0.0", "--port", "8765"]
