FROM python:3.14-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/tesla-invoices

WORKDIR /opt/tesla-invoices

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 9000

# The same image serves both deployments: standalone Docker (configured via
# environment variables) and the Home Assistant app (the Supervisor mounts
# /data with options.json, which Config.load() picks up automatically).
CMD ["python", "-m", "app.main"]
