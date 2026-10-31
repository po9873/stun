FROM python:3.12-slim

# 国内网络构建默认走清华镜像，避免 PyPI / Debian 官方源超时（docker 主机在国内部署）。
# 如需换源或走官方源，可覆盖 build-arg：
#   docker build --build-arg PIP_INDEX_URL=https://pypi.org/simple ...
ARG PIP_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple"
ARG PIP_TRUSTED_HOST="pypi.tuna.tsinghua.edu.cn"

WORKDIR /app

COPY requirements.txt .
RUN if [ -n "$PIP_INDEX_URL" ]; then \
      if [ -n "$PIP_TRUSTED_HOST" ]; then \
        pip install --no-cache-dir -r requirements.txt \
          -i "$PIP_INDEX_URL" --trusted-host "$PIP_TRUSTED_HOST"; \
      else \
        pip install --no-cache-dir -r requirements.txt -i "$PIP_INDEX_URL"; \
      fi \
    else \
      pip install --no-cache-dir -r requirements.txt; \
    fi

COPY app ./app
# 探针客户端：单文件、只依赖标准库，由 WebUI 分发给内网机器
COPY probe_client.py ./probe_client.py
# 常驻探针（agent）：装在目标机上，自己拉清单、自己测、自己上报
COPY agent_client.py ./agent_client.py

# 自动把 agent 装到目标机上要用到 ssh / sshpass
ARG APT_MIRROR="mirrors.tuna.tsinghua.edu.cn"
RUN if [ -n "$APT_MIRROR" ]; then \
      sed -i "s|deb.debian.org|$APT_MIRROR|g;s|security.debian.org|$APT_MIRROR|g" \
          /etc/apt/sources.list.d/debian.sources; \
    fi && \
    apt-get update && apt-get install -y --no-install-recommends \
      openssh-client sshpass && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    WEB_PORT=80

RUN mkdir -p /data

EXPOSE 80

# 端口读环境变量 WEB_PORT（默认 80），方便以后改端口不重打镜像
CMD uvicorn app.main:app --host 0.0.0.0 --port "${WEB_PORT:-80}"
