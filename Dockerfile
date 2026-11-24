# syntax=docker/dockerfile:1
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
COPY kestrel ./kestrel
# Optional corporate/proxy CA: docker build --secret id=extra_ca,src=/path/ca.pem
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca; fi; \
    pip install --no-cache-dir .
COPY db ./db
COPY rules ./rules
COPY data ./data
EXPOSE 8000
CMD ["uvicorn", "kestrel.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
