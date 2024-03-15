# syntax=docker/dockerfile:1.6

# https://askubuntu.com/questions/972516/debian-frontend-environment-variable
ARG DEBIAN_FRONTEND=noninteractive

# Download the models
FROM wget_base as rk-models


# Build the Venv
FROM python_base as rk-venv

ARG TARGETARCH

# Use a separate container to build wheels to prevent build dependencies in final image
RUN apt-get -qq update \
    && apt-get -qq install -y \
    apt-transport-https \
    gnupg \
    wget
    # the key fingerprint can be obtained from https://ftp-master.debian.org/keys.html
#RUN wget -qO- "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x4D64FEC119C2029067D6E791F8D2585B8783D481" | \
#    gpg --dearmor > /usr/share/keyrings/debian-archive-bookworm-stable.gpg \
#    && echo "deb [signed-by=/usr/share/keyrings/debian-archive-bookworm] http://deb.debian.org/debian bookworm main contrib non-free" | \
#    tee /etc/apt/sources.list.d/debian-bookworm-nonfree.list
RUN sed -i 's/^Components: main$/& contrib non-free/' /etc/apt/sources.list.d/debian.sources
RUN apt-get update
RUN apt-get install -y \
    # opencv dependencies
    build-essential cmake git pkg-config libgtk-3-dev \
    libavcodec-dev libavformat-dev libswscale-dev libv4l-dev \
    libxvidcore-dev libx264-dev libjpeg-dev libpng-dev libtiff-dev \
    gfortran openexr libatlas-base-dev libssl-dev\
    libtbbmalloc2 \
    libtbb-dev libdc1394-dev libopenexr-dev \
    libgstreamer-plugins-base1.0-dev libgstreamer1.0-dev \
    # scipy dependencies
    gcc gfortran libopenblas-dev liblapack-dev && \
    rm -rf /var/lib/apt/lists/*


#install the requirements
COPY docker_build_files/base/requirements.txt /requirements.txt
RUN pip3 install -r /requirements.txt

COPY docker_build_files/base/requirements-wheels.txt /requirements-wheels.txt
COPY docker_build_files/rockchip/requirements-wheels-rk.txt /requirements-wheels-rk.txt
RUN sed -i "/https:\/\//d" /requirements-wheels.txt
RUN pip3 wheel --wheel-dir=/rk-wheels -c /requirements-wheels.txt -r /requirements-wheels-rk.txt

RUN ls /opt/virtualenvs/aispy/lib/python3.11/site-packages
RUN ls /opt/virtualenvs/aispy/lib64/python3.11/site-packages
#
#
#
## AISpy deps (ffmpeg, python, nginx, go2rtc, s6-overlay, etc)
#FROM python_base AS rk-basewithdeps
#ARG TARGETARCH
#
#ARG DEBIAN_FRONTEND
## http://stackoverflow.com/questions/48162574/ddg#49462622
#ARG APT_KEY_DONT_WARN_ON_DANGEROUS_USAGE=DontWarn
#
### https://github.com/NVIDIA/nvidia-docker/wiki/Installation-(Native-GPU-Support)
##ENV NVIDIA_VISIBLE_DEVICES=all
##ENV NVIDIA_DRIVER_CAPABILITIES="compute,video,utility"
#
##ENV PATH="/usr/lib/btbn-ffmpeg/bin:/usr/local/go2rtc/bin:/usr/local/nginx/sbin:${PATH}"
#
## Install dependencies
#
#
## Copy Virtualenvironment
#COPY --from=rk-venv /opt/virtualenvs/aispy /opt/virtualenvs/aispy
#
#RUN ldconfig
#
#
#
#
## Collect project files in a single layer
#FROM scratch AS rootfs
#
#WORKDIR /opt/aispy/
#COPY aispy aispy/
##COPY migrations migrations/
##COPY --from=web-build /work/dist/ web/
#
#
## Final container
#FROM rk-basewithdeps AS AISpy
#
#WORKDIR /opt/aispy/
#COPY --from=rootfs / /
#
#CMD ["python3"]