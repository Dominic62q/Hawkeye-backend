# Builds hawkeye-ui and serves it from Caddy. The build context is the
# deployment directory, with the UI source in ./ui.
FROM node:22-alpine AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
COPY ui/ ./
RUN npm run build

FROM caddy:2-alpine
COPY --from=ui /ui/dist /srv
COPY Caddyfile /etc/caddy/Caddyfile
