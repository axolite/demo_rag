# Pinned toolchain image for building the NCS v1.6.1 documentation.
#
# Ubuntu 18.04 is chosen deliberately: it ships the exact doxygen (1.8.13) and
# mscgen (0.20) the 2021 NCS doc build expects. Python 3.8 comes from deadsnakes
# (bionic itself ships 3.6). cmake/ninja/west come from pip because bionic's apt
# cmake (3.10) is too old for the doc CMakeLists (needs >=3.17).
#
# The image is corpus-version-agnostic: it carries only the toolchain. The
# actual sources are cloned fresh with west at run time and the Python doc
# requirements are installed from those sources (see docker/build-docs.sh), so
# nothing here pins NCS itself.
#
#   docker build -t ncs161-docs -f docker/ncs-1.6.1-docs.Dockerfile docker/
#
# Confirm in the build log:  doxygen --version  ->  1.8.13
FROM ubuntu:18.04

ENV DEBIAN_FRONTEND=noninteractive

# bionic is past standard EOL; its LTS pockets remain on archive.ubuntu.com, but
# fall back to old-releases if a mirror has dropped them.
RUN ( apt-get update || ( \
        sed -i 's|//archive.ubuntu.com|//old-releases.ubuntu.com|g; s|//security.ubuntu.com|//old-releases.ubuntu.com|g' \
            /etc/apt/sources.list && apt-get update ) ) \
    && apt-get install -y --no-install-recommends \
        doxygen mscgen graphviz \
        git build-essential curl ca-certificates \
        software-properties-common gnupg \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        python3.8 python3.8-venv python3.8-dev \
    && rm -rf /var/lib/apt/lists/*

# Isolated Python; pip-provided cmake/ninja/west land on PATH ahead of apt's.
RUN python3.8 -m venv /venv
ENV PATH=/venv/bin:$PATH
RUN pip install --no-cache-dir -U pip wheel "cmake>=3.20" ninja west

# build-docs.sh and constraints.txt are *mounted* at /work (not COPYed) so they
# can be iterated without rebuilding the image.
WORKDIR /work
CMD ["bash", "/work/build-docs.sh"]
