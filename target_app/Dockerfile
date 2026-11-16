FROM python:3.12-slim
WORKDIR /srv
RUN pip install --no-cache-dir fastapi==0.115.6 uvicorn==0.32.1 prometheus-client==0.21.1 httpx==0.28.1 \
    "psycopg[binary]==3.2.3" redis==5.2.1
COPY target_app /srv/target_app
ENV PYTHONUNBUFFERED=1
CMD ["sh", "-c", "uvicorn target_app.app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --log-level warning"]
