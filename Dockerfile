FROM python:3.12-slim

# 安装 Playwright Chromium 所需的系统库
RUN apt-get update && apt-get install -y \
    curl wget gnupg ca-certificates \
    libasound2 libatk-bridge2.0-0 libatk1.0-0 libcups2 \
    libdrm2 libgbm1 libgtk-3-0 libnspr4 libnss3 \
    libu2f-udev libxcomposite1 libxdamage1 libxfixes3 \
    libxkbcommon0 libxrandr2 xdg-utils \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 装 Chromium 到 /opt（所有用户可读）
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN python -m playwright install chromium
RUN chmod -R a+r /opt/ms-playwright

# 复制应用代码
COPY app.py .
COPY templates/ ./templates/

# 非 root 用户运行
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
CMD ["sh", "-c", "python -m uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
