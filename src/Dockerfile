# Everything this build needs is in the repository, in vendor/, so
# `docker compose up` works on a machine with no internet at all: no image is
# pulled from Docker Hub and no package comes from PyPI. vendor/README.md says
# what each file is and how it was made. docker-compose.yml also builds with
# networking switched off, so a missing file stops the build instead of being
# quietly downloaded.
#
# No "# syntax=" line on purpose: that would make Docker fetch a frontend image.

# The base system: the official python:3.12-slim image (Python 3.12.14 on
# Debian trixie), unpacked from a file instead of pulled. Docker picks the
# file for this machine's processor (amd64 or arm64).
FROM scratch AS base
ARG TARGETARCH
ADD vendor/python-3.12-slim-${TARGETARCH}.tar.bz2 /
ENV PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    LANG=C.UTF-8 \
    PYTHON_VERSION=3.12.14 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# The Python packages, from the wheels in vendor/wheels and nowhere else.
FROM base AS packages
COPY requirements.txt /tmp/requirements.txt
COPY vendor/wheels /tmp/wheels
RUN pip install --no-index --find-links=/tmp/wheels --prefix=/install -r /tmp/requirements.txt

# The application without vendor/, which the running portal doesn't need.
FROM base AS source
COPY . /app
RUN rm -rf /app/vendor

FROM base
ENV DATA_DIR=/data
WORKDIR /app
COPY --from=packages /install /usr/local
COPY --from=source /app /app

# Static files are collected at build time, so the running container never
# fetches anything. The throwaway DATA_DIR keeps the build from writing a
# secret key into the image.
RUN DATA_DIR=/tmp/build-data python manage.py collectstatic --noinput --verbosity 0 \
 && rm -rf /tmp/build-data \
 && useradd --no-log-init --uid 10001 --home-dir /app --shell /usr/sbin/nologin shipshape \
 && mkdir -p /data \
 && chown shipshape /data

USER shipshape
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=10s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"

CMD ["python", "boot.py"]
