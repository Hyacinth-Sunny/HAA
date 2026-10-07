# p2-sandbox-base —— P2 实验容器沙箱基础镜像（大修第一章 §6.5：预构建入库）
# 构建：docker build -f docker/p2-sandbox-base.Dockerfile -t p2-sandbox-base:latest docker/
# 变更走版本号（tag），不覆盖 latest 语义。
FROM python:3.12-slim

# 基础科学计算栈（P2 实验默认环境；CUDA 变体按目标机另建 tag）
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir \
        numpy pandas matplotlib scipy scikit-learn

# solve.sh 契约的运行前提：bash + python 可用，工作目录由运行时挂载 /workspace
WORKDIR /workspace

# 默认非 root 运行（容器内写权限限定在挂载卷）
RUN useradd -m sandbox && chown -R sandbox /workspace
USER sandbox

CMD ["/bin/bash", "-lc", "echo p2-sandbox-base ready"]
