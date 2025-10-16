# Build stage pull official base image
FROM python:3.13-slim-bookworm

# Define a build-time variable
ARG ENV=dev
ARG APP_GID=1000
ARG APP_UID=1000
ARG DEBUG=False

# Use an ENV command to set the environment variable using the content of the file
ENV ENV="${ENV:-dev}"
ENV DEBUG="${DEBUG:-False}"
ARG APP_GID="${APP_GID:-1000}"
ARG APP_UID="${APP_UID:-1000}"
ARG DEBIAN_FRONTEND=noninteractive

# [Python Interpreter Flags
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHON_VERSION=3.11 \
    PYTHONIOENCODING=UTF-8 \
    PIP_NO_CACHE_DIR=off \
    VENV_DIR=/home/site/wwwroot/venv \
    APP_HOME=/home/site/wwwroot \
    APP_ROOT=/home/site/wwwroot/src \
    APP_LOGS=/var/log/app-wwwroot \
    APP_SECURE=/var/secure/app-wwwroot \
    APP_UID=${APP_UID:-1000} \
    APP_GID=${APP_GID:-1000}

RUN export LC_ALL=en_US.UTF-8 && \
    export LANG=en_US.UTF-8 && \
    export LANGUAGE=en_US.UTF-8

# Compiler and OS libraries
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    locales \
    ca-certificates \
    openssl \
    libssl-dev \
    libffi-dev \
    lsb-release \
    python3-pip && \
    locale-gen en_US.UTF-8

# Upgrade pip and setuptools
RUN pip3 install --upgrade pip && \
    pip3 install --upgrade setuptools

# Install custom scripts
COPY scripts/fix-permissions.sh /usr/bin/fix-permissions
RUN chmod +x /usr/bin/fix-permissions

# User Creation - It’s a good idea to not run the app as root.
RUN groupadd --system \
    --gid=$APP_GID appgroup && \
    useradd --system \
    --no-create-home \
    --shell=/bin/bash \
    --home=$APP_HOME \
    --gid=$APP_GID \
    --uid=$APP_UID \
    appuser

# Change root and app user password
RUN echo "root:`tr -dc A-Za-z0-9_ < /dev/urandom | head -c 16 | xargs`" | chpasswd & \
    echo "appuser:`tr -dc A-Za-z0-9_ < /dev/urandom | head -c 16 | xargs`" | chpasswd

# Project libraries definition
COPY ./requirements.txt /tmp/requirements.txt

# Install project libraries
RUN python -m venv ${VENV_DIR} && \
    ${VENV_DIR}/bin/pip install --upgrade pip && \
    ${VENV_DIR}/bin/pip install --upgrade setuptools>=69.0.3 && \
    ${VENV_DIR}/bin/pip install --no-cache-dir -r /tmp/requirements.txt

# Remove APT cache
RUN rm -rf /tmp && \
    rm -rf /var/lib/apt/lists/* && \
    apt-get -y autoclean && \
    apt-get -y clean && \
    apt-get -y autoremove

# Copy source code into ./src
COPY ./src ${APP_ROOT}

# Add correct path for the  Python virtual environment
ENV PATH="${VENV_DIR}/bin:$PATH"

# Copy container entrypoint script
COPY scripts/container-entrypoint.sh /usr/bin/container-entrypoint
RUN chmod +x /usr/bin/container-entrypoint

# Copy the health check script into the container
COPY scripts/healthcheck.sh /usr/bin/healthcheck.sh
RUN chmod +x /usr/bin/healthcheck.sh

# Health check to ensure the python main.py process is running
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD /usr/bin/healthcheck.sh

# Fix application files permissions
RUN mkdir -p ${APP_ROOT} && \
    bash fix-permissions $APP_HOME && \
    chown -R ${APP_UID}:${APP_GID} ${APP_ROOT} && \
    chown -R ${APP_UID}:${APP_GID} ${APP_LOGS} && \
    find ${APP_ROOT} -type f -exec chmod 644 {} \; || true && \
    find ${APP_ROOT} -type d -exec chmod 755 {} \; || true && \
    mkdir -p ${APP_LOGS} && \
    chmod -R 755 ${APP_LOGS}

# Add virtual environment activation the shell
# Use a non-interactive shell, so we need to add venv to .bashrc for interactive sessions
RUN echo 'source ${VENV_DIR}/bin/activate' >> ${HOME}/.bashrc

WORKDIR ${APP_ROOT}

#  Switch to app user and expose port 8000
USER ${APP_UID}

ENTRYPOINT ["/bin/bash", "container-entrypoint"]
