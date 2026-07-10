FROM node:20-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build && npm prune --omit=dev

FROM node:20-alpine AS runtime
WORKDIR /app
# Harden the runtime image against the base-image HIGH CVEs Trivy flags:
#  - `apk upgrade` patches OS packages (e.g. OpenSSL libcrypto3/libssl3).
#  - remove the globally-bundled npm: the runtime only runs `node`, and npm's
#    vendored deps (tar, sigstore, glob, minimatch, cross-spawn) are the source
#    of the image's remaining HIGH CVEs. Neither npm nor npx is needed to serve.
RUN apk upgrade --no-cache \
 && rm -rf /usr/local/lib/node_modules/npm /usr/local/bin/npm /usr/local/bin/npx
RUN addgroup -S appgroup && adduser -S appuser -G appgroup
COPY --from=builder /app/dist ./dist
COPY --from=builder /app/node_modules ./node_modules
USER appuser
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=3s CMD wget -q -O- http://localhost:3000/health || exit 1
CMD ["node", "dist/index.js"]
