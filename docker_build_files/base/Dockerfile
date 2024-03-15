# syntax=docker/dockerfile:1.6

# https://askubuntu.com/questions/972516/debian-frontend-environment-variable
ARG DEBIAN_FRONTEND=noninteractive
ARG BASE_IMAGE=debian:12
ARG SLIM_BASE=debian:12-slim
ARG PYTHON_VERSION=3.11

# Create the various bases
FROM ${BASE_IMAGE} AS base
ARG DEBIAN_FRONTEND

FROM ${SLIM_BASE} AS slim_base
ARG DEBIAN_FRONTEND

FROM --platform=${BUILDPLATFORM} ${BASE_IMAGE} AS host_base
ARG DEBIAN_FRONTEND

# Create a base with python to be used for the virtual environment
FROM slim_base as python_base
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    python3.11 \
    python3-pip \
    python3.11-venv \
    python3.11-dev

# Setup the virtualenv
RUN python3 -m venv /opt/virtualenvs/aispy
ENV PATH="/opt/virtualenvs/aispy/bin:$PATH"


# Create a stage with wget
FROM slim_base AS wget_base
RUN apt-get update \
    && apt-get install -y wget xz-utils \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /rootfs





