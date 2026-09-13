# LoomVec 用户端静态镜像（多阶段：构建 → Nginx 托管）
# 构建上下文 = 仓库根：docker build -f apps/web/Dockerfile .
FROM node:22-alpine AS build
WORKDIR /app
RUN corepack enable
# 契约产物未随上下文提供时，用 stub 保证可独立构建（CI 的 contract-sdk job 保证真实同步）
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY packages/sdk-ts packages/sdk-ts
COPY packages/ui packages/ui
COPY apps/web apps/web
RUN pnpm install --frozen-lockfile
RUN pnpm --filter @loomvec/web build

FROM nginx:alpine
COPY --from=build /app/apps/web/dist /usr/share/nginx/html
COPY apps/web/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
