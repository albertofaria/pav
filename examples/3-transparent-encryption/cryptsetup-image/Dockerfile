# ---------------------------------------------------------------------------- #

FROM debian:buster-slim AS builder

ARG version

RUN apt-get update && \
    apt-get install --yes autoconf automake autopoint curl gcc gettext \
    libargon2-0-dev libblkid-dev libdevmapper-dev libjson-c-dev libpopt-dev \
    libpwquality-dev libsepol1-dev libssh-dev libssl-dev libtool make \
    pkg-config tar uuid-dev

ADD "https://gitlab.com/cryptsetup/cryptsetup/-/archive/${version}/cryptsetup-${version}.tar.gz" /
RUN tar -C / -xf "cryptsetup-${version}.tar.gz"

WORKDIR "/cryptsetup-${version}"
RUN ./autogen.sh
RUN ./configure --prefix=/usr/local
RUN make install

# ---------------------------------------------------------------------------- #

FROM debian:buster-slim

RUN apt-get update && \
    apt-get install --yes libdevmapper1.02.1 libjson-c-dev libpopt0 libssl1.1

COPY --from=builder /usr/local/sbin/ /usr/local/sbin/
COPY --from=builder /usr/local/lib/ /usr/local/lib/
RUN ldconfig

ENTRYPOINT [ "cryptsetup" ]

# ---------------------------------------------------------------------------- #
