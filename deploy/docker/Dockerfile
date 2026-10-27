# syntax=docker/dockerfile:1
#
# The flex-orchestrator image (PLAN.md M6): environment.yml (Python, flex-pse,
# Acquirium, solvers) plus this package. One image runs every part of a tenant
# stack: the orchestrator (`flex-orch`, `python script.py`), the tenant's
# Acquirium server (`acquirium server`) and Ray nodes (`ray start`).
#
#   docker build -f deploy/docker/Dockerfile -t flexorch:0.1 .
#
# It runs as the non-root user flexorch (uid 1000), and works with a read-only
# root filesystem: only /data (a volume), /work and /tmp are written.
FROM mambaorg/micromamba:2.3.3

USER root
# Rename the image's user and give it uid/gid 1000, as the micromamba image
# documents, so volumes and secret files line up with the tenant stack's.
ARG NEW_MAMBA_USER=flexorch
ARG NEW_MAMBA_USER_ID=1000
ARG NEW_MAMBA_USER_GID=1000
RUN usermod "--login=${NEW_MAMBA_USER}" "--home=/home/${NEW_MAMBA_USER}" \
        --move-home "-u ${NEW_MAMBA_USER_ID}" "${MAMBA_USER}" \
    && groupmod "--new-name=${NEW_MAMBA_USER}" "-g ${NEW_MAMBA_USER_GID}" "${MAMBA_USER}" \
    && echo "${NEW_MAMBA_USER}" > /etc/arg_mamba_user \
    && mkdir -p /data /work \
    && chown "${NEW_MAMBA_USER_ID}:${NEW_MAMBA_USER_GID}" /data /work
ENV MAMBA_USER=${NEW_MAMBA_USER}
USER ${MAMBA_USER}

COPY --chown=${MAMBA_USER}:${MAMBA_USER} environment.yml /tmp/environment.yml
RUN micromamba install --yes --name base --file /tmp/environment.yml \
    && micromamba clean --all --yes

# Later RUN steps run inside the env.
ARG MAMBA_DOCKERFILE_ACTIVATE=1
# IPOPT and the other IDAES solver binaries (PLAN.md §6).
RUN idaes get-extensions

COPY --chown=${MAMBA_USER}:${MAMBA_USER} pyproject.toml README.md /opt/flex-orchestrator/
COPY --chown=${MAMBA_USER}:${MAMBA_USER} src /opt/flex-orchestrator/src
RUN pip install --no-cache-dir "/opt/flex-orchestrator[server]"

# The env is on PATH without the entrypoint too: docker exec and health checks
# don't go through it.
ENV PATH=/opt/conda/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    RAY_USAGE_STATS_ENABLED=0 \
    ACQUIRIUM_EXACT_ONLY=true

WORKDIR /work
CMD ["flex-orch", "--help"]
