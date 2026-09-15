# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Reviewer editor. Built with Vite, served as static files by nginx, which also
# reverse-proxies /api to the backend so the browser stays same-origin.
# ---------------------------------------------------------------------------

FROM node:22-alpine AS builder

WORKDIR /build
COPY package.json package-lock.json* ./
RUN npm ci || npm install

COPY . .
RUN npm run build


FROM nginx:1.27-alpine AS runtime

COPY --from=builder /build/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD wget -q --spider http://127.0.0.1/ || exit 1
