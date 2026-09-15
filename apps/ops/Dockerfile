# LoomVec 运营端静态镜像（多阶段：构建 → Nginx 托管）
# 构建上下文 = 仓库根：docker build -f apps/ops/Dockerfile .
FROM node:22-alpine AS build
WORKDIR /app
RUN corepack enable
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY packages/sdk-ts packages/sdk-ts
COPY packages/ui packages/ui
COPY apps/ops apps/ops
RUN pnpm install --frozen-lockfile
RUN pnpm --filter @loomvec/ops build

FROM nginx:alpine
COPY --from=build /app/apps/ops/dist /usr/share/nginx/html
COPY apps/ops/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
