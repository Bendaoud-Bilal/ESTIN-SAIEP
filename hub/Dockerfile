# SAIEP JupyterHub image (runs in-cluster; KubeSpawner creates notebook Pods).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    READTHEDOCS=1 \
    PIP_NO_CACHE_DIR=1

# Node is needed by JupyterHub's configurable-http-proxy. PAM headers are
# needed by nativeauthenticator's pamela dependency.
RUN apt-get update && apt-get install -y --no-install-recommends \
        nodejs npm build-essential libpam0g-dev \
    && npm install -g configurable-http-proxy \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Dependency layer: keep it cached when only SAIEP code changes.
COPY requirements.txt pyproject.toml setup.py MANIFEST.in README.md /app/
RUN pip install -r requirements.txt \
    && pip install jupyterhub-nativeauthenticator docker kubernetes rich pyyaml

RUN useradd --create-home --uid 1000 --shell /bin/bash hub \
    && mkdir -p /srv/jupyterhub

# READTHEDOCS=1 skips the frontend npm/css/jsx build; this fork ships its
# built static assets in share/jupyterhub.
COPY . /app
RUN pip install --no-deps . \
    && chown -R hub:hub /srv/jupyterhub /app

USER hub
ENV HOME=/home/hub
WORKDIR /app
EXPOSE 8000 8081
CMD ["jupyterhub", "-f", "/app/jupyterhub_config.py"]
