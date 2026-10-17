FROM python:3.11-slim

# 建立 Hugging Face Spaces 標準非 root 使用者 (UID 1000) 以確保 SQLite 寫入與解壓縮權限
RUN useradd -m -u 1000 user

WORKDIR /app

COPY --chown=user requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt

COPY --chown=user . /app

USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1

WORKDIR /app/backend_mvp

EXPOSE 7860

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]
