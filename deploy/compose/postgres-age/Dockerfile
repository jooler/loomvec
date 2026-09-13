# PostgreSQL 18 + Apache AGE 自建镜像
# AGE 自 2026-01 版本起支持 PG18（release notes: https://age.apache.org/release-notes/），
# 官方 Docker 镜像尚未覆盖 PG18，故从源码构建扩展。
# AGE 源码以宿主机克隆的 age-src/ 作为构建上下文注入（避免容器内 git clone 的网络/CA 问题），
# 更新方式：git clone --depth 1 --branch PG18 https://github.com/apache/age.git age-src
FROM postgres:18

COPY age-src /tmp/age

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential flex bison postgresql-server-dev-18 \
    && cd /tmp/age \
    && make -j"$(nproc)" && make install \
    && cd / && rm -rf /tmp/age \
    && apt-get purge -y build-essential flex bison postgresql-server-dev-18 \
    && apt-get autoremove -y
