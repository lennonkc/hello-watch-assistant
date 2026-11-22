# Online Q&A service image (Cloud Run): FastAPI + ADK agent + static web front end.
# Build context = repository root (deploy/web/deploy.sh packs the committed code with git archive).
# Knowledge packs are NOT in the image: they are pulled from Cloud Storage at startup. The agent prompts are: the
# published prompts/agent/ is copied in and is what the service runs (an optional secret can still override it).
FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements-web.txt ./
RUN pip install -r requirements-web.txt
COPY pyproject.toml ./
COPY catalog ./catalog
COPY schema ./schema
COPY ingest ./ingest
COPY prompts ./prompts
COPY server ./server
COPY web ./web
# a failing test fails the build: pytest's own exit status, no pipe (pinned by tools/ops/tests/test_run_sh.py)
RUN python -m pytest -q -p no:cacheprovider server/tests
ENV PORT=8080
CMD ["sh", "-c", "exec uvicorn server.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
