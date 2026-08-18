FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip install --no-cache-dir .

USER 65532:65532
EXPOSE 8000
CMD ["hcuopt", "api", "--host", "0.0.0.0", "--port", "8000"]
