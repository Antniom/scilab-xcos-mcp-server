FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=7860 \
    XCOS_SERVER_MODE=http \
    XCOS_VALIDATION_MODE=subprocess

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        autoconf \
        gcc \
        gfortran \
        default-jdk \
        libxml2-dev \
        libhdf5-dev \
        xvfb \
        xauth \
        libgl1-mesa-glx \
        libxtst6 \
        libxi6 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

# Compile the bundled Scilab source
RUN cd /app/scilab-2026.0.1/scilab && \
    ./configure && \
    make && \
    make install


RUN chown -R user:user /app && \
    pip install .

USER user
ENV HOME=/home/user

EXPOSE 7860

CMD ["python", "server.py"]
