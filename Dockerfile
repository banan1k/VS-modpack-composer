FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .
COPY app ./app
COPY tests ./tests
COPY .env.example ./
RUN mkdir -p /app/data/cache/images /app/data/cache/files /app/data/cache/archives
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
