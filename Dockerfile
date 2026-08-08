FROM python:3.12-slim-bookworm

# ffmpeg:
#   - merges separate video/audio streams
#   - extracts MP3/M4A audio
#
# curl + unzip + ca-certificates:
#   - required for installing Deno
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        unzip \
        ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Hugging Face Docker Spaces run as UID 1000.
RUN useradd -m -u 1000 user && \
    mkdir -p /data && \
    chmod 777 /data

USER user

ENV HOME=/home/user
ENV PATH=/home/user/.local/bin:/home/user/.deno/bin:$PATH

WORKDIR /home/user/app

# Install Deno.
# yt-dlp currently recommends Deno for YouTube's JS challenges.
RUN curl -fsSL https://deno.land/install.sh | sh

# Generate YouTube proof-of-origin tokens without account cookies. The matching
# Python plugin is installed from requirements.txt below.
ARG BGUTIL_VERSION=1.3.1
RUN curl -fsSL \
        "https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/tags/${BGUTIL_VERSION}.tar.gz" \
        -o /tmp/bgutil-provider.tar.gz && \
    tar -xzf /tmp/bgutil-provider.tar.gz -C /home/user && \
    mv "/home/user/bgutil-ytdlp-pot-provider-${BGUTIL_VERSION}" \
        /home/user/bgutil-ytdlp-pot-provider && \
    rm /tmp/bgutil-provider.tar.gz && \
    cd /home/user/bgutil-ytdlp-pot-provider/server && \
    deno install --allow-scripts=npm:canvas --frozen

COPY --chown=user requirements.txt .

RUN pip install --user --no-cache-dir -r requirements.txt

COPY --chown=user main/ ./main/

CMD ["python", "-m", "uvicorn", "main.backend.app:app", \
     "--host", "0.0.0.0", \
     "--port", "7860"]
