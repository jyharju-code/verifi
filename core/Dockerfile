# Verifi core engine image. Build context is the repo root.
# Runs the core API by default; the bot service overrides the command.
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY core/ core/

CMD ["uvicorn", "core.api.server:app", "--host", "0.0.0.0", "--port", "8700"]
