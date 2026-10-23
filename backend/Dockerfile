FROM python:3.12-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt -i https://mirrors.cloud.tencent.com/pypi/simple

RUN useradd --create-home --uid 10001 appuser
COPY --chown=appuser:appuser app/ /app/

USER appuser

EXPOSE 8000

CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
