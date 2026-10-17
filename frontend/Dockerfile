# syntax=docker/dockerfile:1

# Next.js frontend: multi-stage build, standalone output, non-root runtime.
#   docker build -t eka-frontend frontend
#   docker run -p 3000:3000 -e API_INTERNAL_URL=http://api:8081 eka-frontend
ARG NODE_VERSION=24

FROM node:${NODE_VERSION}-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json .npmrc ./
RUN npm ci

FROM node:${NODE_VERSION}-alpine AS build
WORKDIR /app
# next.config.ts bakes this into the build; docker-entrypoint.mjs re-applies API_INTERNAL_URL at start-up.
ARG API_INTERNAL_URL=http://localhost:8081
ENV NEXT_TELEMETRY_DISABLED=1 \
    API_INTERNAL_URL=${API_INTERNAL_URL}
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:${NODE_VERSION}-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0 \
    API_INTERNAL_URL=http://localhost:8081
RUN addgroup -S -g 1001 app && adduser -S -u 1001 -G app app
COPY --from=build --chown=app:app /app/.next/standalone ./
COPY --from=build --chown=app:app /app/.next/static ./.next/static
COPY --from=build --chown=app:app /app/public ./public
COPY --chown=app:app docker-entrypoint.mjs ./
USER app
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
  CMD wget -q -O /dev/null "http://127.0.0.1:${PORT}/healthz" || exit 1
ENTRYPOINT ["node", "docker-entrypoint.mjs"]
