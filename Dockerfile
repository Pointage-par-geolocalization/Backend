FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && addgroup --system app && adduser --system --ingroup app app
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini
RUN chown -R app:app /app
USER app
EXPOSE 4000
HEALTHCHECK --interval=20s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4000/ready', timeout=3)" || exit 1
CMD ["sh", "-c", "if [ \"${RUN_MIGRATIONS:-false}\" = \"true\" ]; then alembic upgrade head || exit $?; fi; exec uvicorn app.main:app --host 0.0.0.0 --port 4000"]
