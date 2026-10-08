# p2-sandbox-base —— P2 实验容器沙箱基础镜像（大修第一章 §6.5：预构建入库）
# 构建：docker build -f docker/p2-sandbox-base.Dockerfile -t p2-sandbox-base:latest docker/
# 变更走版本号（tag），不覆盖 latest 语义。
#
# 镜像源（2026-10-07 排查定案）：本机无 /etc/docker/daemon.json 镜像源配置，
# Docker Hub 直连超时（GFW）。经实测可用镜像源：docker.m.daocloud.io（首选，已验证）
# 与 docker.1panel.live（备选）；docker.1ms.run 不可用。
# 方案 A（已内置）：构建时经 REGISTRY_PREFIX 前缀拉取，无需改守护进程配置：
#   docker build --build-arg REGISTRY_PREFIX=docker.m.daocloud.io/library ...
# 方案 B（一次性根治，需 sudo）：/etc/docker/daemon.json 配 registry-mirrors 后
#   systemctl restart docker，此后可用裸镜像名。
ARG REGISTRY_PREFIX=docker.m.daocloud.io/library
FROM ${REGISTRY_PREFIX}/python:3.12-slim

# 国内网络加速：Debian 源换满沌 + pip 走清华镜像（容器内网络同受墙内限制）
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null \
    || sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list

# 基础科学计算栈（P2 实验默认环境；CUDA 变体按目标机另建 tag）
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir \
        numpy pandas matplotlib scipy scikit-learn \
        -i https://pypi.tuna.tsinghua.edu.cn/simple

# solve.sh 契约的运行前提：bash + python 可用，工作目录由运行时挂载 /workspace
WORKDIR /workspace

# 默认非 root 运行（容器内写权限限定在挂载卷）
RUN useradd -m sandbox && chown -R sandbox /workspace
USER sandbox

CMD ["/bin/bash", "-lc", "echo p2-sandbox-base ready"]
