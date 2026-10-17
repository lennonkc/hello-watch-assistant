# Ingest pipeline image: `python -m ingest …` (the Cloud Run Job entry point is `python -m ingest cloud run`).
# Build context = repository root (deploy/ingest/build.sh packs the committed code with git archive).
#
# Pinned versions: the bytes of segment files (x264), thumbnails (OpenCV + Pillow) and pack.db (SQLite) all depend on tool versions,
# so instead of the distro's ffmpeg / Python we pin the same versions as the local ingest environment (exports are byte-identical
# across platforms; media stages still show floating-point / SIMD rounding differences across CPU architectures, see docs/cloud-deploy.md §1):
#   - x264 stable r3222 (b35605a) + FFmpeg 9.0.2, statically built from source (Dockerfile.ffmpeg; base image passed in via FFMPEG_IMAGE);
#   - CPython 3.13.12 (uv / python-build-standalone, bundles SQLite 3.50.4);
#   - Python packages: see requirements-cloud.txt (exact versions).

ARG DEBIAN_TAG=trixie-slim

ARG FFMPEG_IMAGE
FROM ${FFMPEG_IMAGE} AS ffmpeg

FROM debian:${DEBIAN_TAG}
COPY --from=ghcr.io/astral-sh/uv:0.10.2 /uv /usr/local/bin/uv
COPY --from=ffmpeg /opt/ffmpeg/bin/ffmpeg /opt/ffmpeg/bin/ffprobe /usr/local/bin/
ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_LINK_MODE=copy \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
COPY deploy/ingest/requirements-cloud.txt deploy/ingest/overrides-cloud.txt /tmp/
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates zlib1g media-types \
    && rm -rf /var/lib/apt/lists/* \
    && uv venv /opt/venv --python 3.13.12 \
    && uv pip install --python /opt/venv/bin/python --no-cache -r /tmp/requirements-cloud.txt --override /tmp/overrides-cloud.txt \
    && python -c "import sqlite3, cv2, PIL, scenedetect, google.genai, google.cloud.storage; \
print('python', __import__('sys').version.split()[0], 'sqlite', sqlite3.sqlite_version, 'cv2', cv2.__version__, 'PIL', PIL.__version__)" \
    && python -c "import mimetypes; assert mimetypes.guess_type('a.flac')[0] and mimetypes.guess_type('a.mp4')[0]" \
    && ffmpeg -hide_banner -version | head -1

WORKDIR /app
COPY pyproject.toml ./
COPY catalog ./catalog
COPY schema ./schema
COPY ingest ./ingest
ARG GIT_COMMIT=unknown
ENV CEDAR_INGEST_IMAGE_COMMIT=${GIT_COMMIT}
RUN python -m pytest -q ingest/tests ingest/cloud/tests && rm -rf .pytest_cache

ENTRYPOINT ["python", "-m", "ingest"]
CMD ["--help"]
