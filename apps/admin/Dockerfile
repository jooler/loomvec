# LoomVec 运维端静态镜像（多阶段：构建 → Nginx 托管）
# 构建上下文 = 仓库根：docker build -f apps/admin/Dockerfile .
FROM node:22-alpine AS build
WORKDIR /app
RUN corepack enable
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY packages/sdk-ts packages/sdk-ts
COPY packages/ui packages/ui
COPY apps/admin apps/admin
RUN pnpm install --frozen-lockfile
RUN pnpm --filter @loomvec/admin build

FROM nginx:alpine
COPY --from=build /app/apps/admin/dist /usr/share/nginx/html
COPY apps/admin/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
