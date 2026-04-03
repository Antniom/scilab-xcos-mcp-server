FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=7860 \
    XCOS_SERVER_MODE=http \
    XCOS_VALIDATION_MODE=subprocess \
    SCILAB_VERSION=2026.0.1 \
    SCILAB_INSTALL_DIR=/opt/scilab

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        default-jdk \
        curl \
        ca-certificates \
        xz-utils \
        xvfb \
        xauth \
        libgl1-mesa-glx \
        libxtst6 \
        libxi6 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

RUN curl -fsSL "https://www.scilab.org/download/${SCILAB_VERSION}/scilab-${SCILAB_VERSION}.bin.linux-x86_64.tar.xz" -o /tmp/scilab.tar.xz && \
    mkdir -p "${SCILAB_INSTALL_DIR}" && \
    tar -xJf /tmp/scilab.tar.xz -C "${SCILAB_INSTALL_DIR}" && \
    ln -s "${SCILAB_INSTALL_DIR}/scilab-${SCILAB_VERSION}/bin/scilab" /usr/local/bin/scilab && \
    ln -s "${SCILAB_INSTALL_DIR}/scilab-${SCILAB_VERSION}/bin/scilab-cli" /usr/local/bin/scilab-cli && \
    rm -f /tmp/scilab.tar.xz

RUN chown -R user:user /app && \
    pip install .

USER user
ENV HOME=/home/user

EXPOSE 7860

CMD ["python", "server.py"]
