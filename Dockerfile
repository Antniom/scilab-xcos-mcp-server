FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=7860 \
    XCOS_SERVER_MODE=http \
    XCOS_VALIDATION_MODE=subprocess

RUN apt-get update && \
    apt-get install -y --no-install-recommends scilab xvfb libgl1-mesa-glx libxtst6 libxi6 && \
    rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 user

WORKDIR /app

COPY . /app

RUN chown -R user:user /app && \
    pip install .

USER user
ENV HOME=/home/user

EXPOSE 7860

CMD ["python", "server.py"]
