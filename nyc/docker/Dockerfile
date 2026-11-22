FROM python:3.11.18-slim-bookworm@sha256:a83ba18aef0f865b1a4a611c2cf460fe446f2795673bbb9fe43be570439e3b39

ARG UID
ARG GID

RUN apt-get update \
 && apt-get install -y --no-install-recommends git openssh-client \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir numpy==2.4.6 scipy==1.17.1 pillow==13.0.0 \
 && groupadd --gid "$GID" rentmap \
 && useradd --uid "$UID" --gid "$GID" --no-create-home --home-dir /tmp --shell /usr/sbin/nologin rentmap

ENV HOME=/tmp
USER rentmap
ENTRYPOINT ["python3", "-I", "-B", "/srv/nyc-rentmap/repo/nyc/collect.py"]
CMD ["status"]
