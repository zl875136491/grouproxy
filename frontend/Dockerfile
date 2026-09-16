FROM node:22-alpine AS dependencies

WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

FROM node:22-alpine AS builder

WORKDIR /app
COPY --from=dependencies /app/node_modules ./node_modules
COPY frontend/ ./

ARG GROUPROXY_BACKEND_API_URL=http://backend:8000
ENV GROUPROXY_BACKEND_API_URL=${GROUPROXY_BACKEND_API_URL}

RUN npm run build

FROM node:22-alpine AS runner

ENV NODE_ENV=production \
    HOSTNAME=0.0.0.0 \
    PORT=80

WORKDIR /app
COPY --from=builder /app/.next/standalone ./

USER node

EXPOSE 80

CMD ["node", "server.js"]
