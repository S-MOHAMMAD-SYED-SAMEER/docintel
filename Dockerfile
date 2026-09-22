# Minimal production image: the FastAPI app and the migrations it needs.
#
# Override PYTHON_IMAGE to build from a registry mirror or an internal
# registry, e.g.
#   docker build --build-arg PYTHON_IMAGE=mirror.gcr.io/library/python:3.13-slim .
ARG PYTHON_IMAGE=python:3.13-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Every runtime dependency ships a manylinux wheel — psycopg[binary],
# pypdfium2, Pillow — so no compiler or system library is needed here.
COPY pyproject.toml README.md ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
# The evaluation harness and its committed dataset, so `python -m evals.run`
# is reproducible inside the image as well as outside it.
COPY evals ./evals
# The deterministic demo entrypoint and its committed fixture data, for the
# same reason: demo/fixtures/invoice_answers.json must be on disk, and only
# a raw copy (not the installed package) guarantees that. See docs/DEMO.md.
COPY demo ./demo

RUN pip install . && rm -rf build docintel.egg-info

# Storage for uploads and rendered pages. Mount a volume here to keep them;
# the default is a container-local directory so the image runs with no setup.
ENV DOCINTEL_STORAGE_DIR=/var/lib/docintel/storage
RUN useradd --system --create-home --uid 10001 docintel \
    && mkdir -p "${DOCINTEL_STORAGE_DIR}" \
    && chown -R docintel:docintel "${DOCINTEL_STORAGE_DIR}" /app
USER docintel

EXPOSE 8000

# DOCINTEL_DATABASE_URL and DOCINTEL_ANTHROPIC_API_KEY must be supplied at
# run time. See .env.example and the "Configuration" section of README.md.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
