FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

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

HEALTHCHECK --interval=15s --timeout=4s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"

CMD ["python", "boot.py"]
