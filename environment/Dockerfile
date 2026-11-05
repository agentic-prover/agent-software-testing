# syntax=docker/dockerfile:1
#
# Agent Software Testing replay image (specification v0.1).
#
# Adapted from the toolchain image used to measure the reference branch totals, with the
# symbolic execution and bitcode tooling removed. The `toolchain` target passes
# environment/verify_image.py. environment/README.md says what has been verified.
#
# Targets
#   toolchain  compilers, lcov, sanitiser runtimes; no program builds (the default,
#              because it is the last stage of this file)
#   builds     toolchain plus the 18 coverage builds and 18 ASan+UBSan builds
#
#   docker build --target toolchain -t agent-software-testing:toolchain -f environment/Dockerfile .
#   docker build --target builds -t agent-software-testing:builds -f environment/Dockerfile .
#
# The `builds` target needs three third-party build inputs (two Krep patches and the
# libmatheval driver). They are in this repository, in environment/third_party/concollmic,
# under their own licences (see environment/README.md, section "Third-party build inputs"),
# and the target copies them into the image. To build with other files instead, give the
# build context of the `assets` stage (BuildKit):
#
#   docker buildx build --target builds --build-context assets=/path/to/assets \
#       -t agent-software-testing:builds -f environment/Dockerfile .

# The base image is pinned by digest (the multi-architecture index of ubuntu:jammy-20251001).
# A newer jammy image does not work with the dated package snapshot
# below (its libc6 is newer than the snapshot's), so keep the image and the snapshot together.
# The same digest is in environment/toolchain.lock.json (`base_image.digest`).
ARG BASE_IMAGE=ubuntu:jammy-20251001@sha256:09506232a8004baa32c47d68f1e5c307d648fdd59f5e7eaa42aaf87914100db3
FROM ${BASE_IMAGE} AS base

# All packages come from one dated snapshot of the Ubuntu archive so that a rebuild
# yields the same compiler, lcov and library versions.
ARG UBUNTU_SNAPSHOT=20260622T070000Z

ENV DEBIAN_FRONTEND=noninteractive \
    LC_ALL=C \
    TZ=UTC

# The base image has no CA bundle, so the HTTPS snapshot cannot be reached at first. TLS
# verification is never switched off. The bootstrap installs ca-certificates from the
# image's own plain-HTTP archive sources; apt checks the signature of the archive index
# and of every package with the keys that ship in the image, whatever the transport. Only
# then are the sources pointed at the HTTPS snapshot, which verifies TLS from the first
# request on. Snapshot indexes can be older than their Valid-Until date, so that check is
# relaxed for the snapshot's apt calls only, on their command line, and is not left behind
# in the image. The snapshot service has outages, so its downloads are retried and a package
# index that still cannot be fetched stops the build. Otherwise apt only warns and goes on
# without that index, and the image could end up with other versions.
RUN set -eux; \
    apt-get update; \
    apt-get -y --no-install-recommends install ca-certificates; \
    sed -i \
      -e "s#http://archive.ubuntu.com/ubuntu/#https://snapshot.ubuntu.com/ubuntu/${UBUNTU_SNAPSHOT}/#g" \
      -e "s#http://security.ubuntu.com/ubuntu/#https://snapshot.ubuntu.com/ubuntu/${UBUNTU_SNAPSHOT}/#g" \
      /etc/apt/sources.list; \
    apt-get -o Acquire::Check-Valid-Until=false -o Acquire::Retries=20 --error-on=any update; \
    apt-get -o Acquire::Check-Valid-Until=false -o Acquire::Retries=20 -y --no-install-recommends install \
      build-essential pkg-config xz-utils bzip2 curl patch file \
      autoconf automake libtool autopoint gettext texinfo bison flex \
      guile-3.0-dev libogg-dev libvorbis-dev \
      lcov python3 \
      clang-15 llvm-15 \
      tzdata; \
    rm -rf /var/lib/apt/lists/*

# tzdata is needed for a faithful build: gnulib's "working mktime" probe (m4/mktime.m4,
# indiana_test) detects the C library's mktime defect only when the zoneinfo database exists. Without it configure
# keeps libc's mktime, lib/mktime.c is not linked into diff, du and ls, and their universes lack its 88 branches.

# Sanitiser runtime configuration. The replay runner sets these for every case of
# the sanitiser pass; cases cannot override them.
ENV ASAN_SYMBOLIZER_PATH=/usr/lib/llvm-15/bin/llvm-symbolizer

# Fixed absolute location of all builds. GCC writes absolute object paths into the
# coverage notes, so a build must be mounted at the path it was compiled at.
ENV AGENT_SOFTWARE_TESTING_BUILDS=/opt/agent-software-testing/builds
RUN useradd --create-home --uid 10001 replay \
 && mkdir -p /opt/agent-software-testing/builds /opt/agent-software-testing/environment /opt/agent-software-testing/programs \
 && chown -R replay:replay /opt/agent-software-testing

COPY --chown=replay:replay environment/build_programs.py environment/verify_image.py environment/toolchain.lock.json /opt/agent-software-testing/environment/
COPY --chown=replay:replay programs/ /opt/agent-software-testing/programs/

USER replay
WORKDIR /home/replay
CMD ["bash"]


# The third-party build inputs of the repository (environment/third_party/concollmic, laid out
# as <program>/<file> with their licence texts). Override with `--build-context assets=<dir>`
# (BuildKit), which replaces this stage; the builder checks the SHA-256 of every file either way.
FROM scratch AS assets
COPY environment/third_party/concollmic/ /


FROM base AS builds
COPY --from=assets / /opt/agent-software-testing/assets/
RUN python3 /opt/agent-software-testing/environment/build_programs.py \
      --definitions /opt/agent-software-testing/programs \
      --out /opt/agent-software-testing/builds \
      --assets /opt/agent-software-testing/assets \
      --all


# Last stage, hence the default target: the toolchain without any program build.
FROM base AS toolchain
