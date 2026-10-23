# SAIEP interactive notebook image.
# KubeSpawner runs this with `jupyterhub-singleuser`; the admission webhook
# overrides the UID/GID to the tenant UID and mounts a per-user home PVC.
FROM quay.io/jupyter/scipy-notebook:latest

USER root
RUN pip install --no-cache-dir "jupyterhub>=5.0" \
    && fix-permissions "${CONDA_DIR}" \
    && fix-permissions "/home/${NB_USER}"

USER ${NB_USER}
WORKDIR /home/${NB_USER}
