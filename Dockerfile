FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 app
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ringbreaker ringbreaker
COPY web web
COPY docs docs
# Build the synthetic world, model and cases at image build time, plus the public static site.
RUN python -m ringbreaker build && python -m ringbreaker export-demo --site dist/site && chown -R app /app
USER app
ENV RINGBREAKER_STATE_DIR=/app/state RINGBREAKER_SITE_DIR=/app/dist/site
EXPOSE 8000
CMD ["python", "-m", "ringbreaker", "serve", "--host", "0.0.0.0", "--port", "8000"]
