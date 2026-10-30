FROM python:3.12-slim

# 国内网络构建时可指定 pip 镜像，避免 PyPI 超时：
#   docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple ...
ARG PIP_INDEX_URL=""
ARG PIP_TRUSTED_HOST=""

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

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    WEB_PORT=80

RUN mkdir -p /data

EXPOSE 80

# 端口读环境变量 WEB_PORT（默认 80），方便以后改端口不重打镜像
CMD uvicorn app.main:app --host 0.0.0.0 --port "${WEB_PORT:-80}"
