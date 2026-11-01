# syntax=docker/dockerfile:1.7
FROM python:3.15-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.15-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 GITPULSE_LOG_FORMAT=json MPLCONFIGDIR=/tmp/mpl
# git is needed for history extraction; nothing else.
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/* && useradd --create-home --uid 10001 gitpulse
WORKDIR /app
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels
COPY --chown=gitpulse gitpulse.yml ./
USER gitpulse
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/healthz').status!=200)"
CMD ["sh", "-c", "gitpulse serve --port ${PORT:-8000}"]
