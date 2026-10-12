# 构建上下文为仓库根目录 (Railway 里把 Dockerfile Path 设为 railway-poller/Dockerfile)
FROM python:3.12-slim
WORKDIR /app
COPY media.py crypto_util.py ./
COPY railway-poller/poller.py ./
CMD ["python3", "poller.py"]
