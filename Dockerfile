FROM python:3.12-slim

# 安装 Playwright 所需的系统依赖
RUN apt-get update && apt-get install -y \
    curl \
    wget \
    gnupg \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libu2f-udev \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 安装 Playwright Chromium（仅安装浏览器，不装系统依赖因为已经装了）
RUN python -m playwright install chromium
RUN python -m playwright install-deps chromium || true

# 复制应用代码
COPY app.py .
COPY templates/ ./templates/

# 创建非 root 用户运行（安全最佳实践）
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# 暴露端口（通过环境变量 PORT 配置，默认 8000）
EXPOSE 8000

CMD ["sh", "-c", "python -m uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
