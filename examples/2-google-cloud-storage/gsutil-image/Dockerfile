# ---------------------------------------------------------------------------- #

FROM python:3.9-slim

ARG version

RUN pip install --no-cache-dir "gsutil==${version}"

ENTRYPOINT [ "gsutil" ]

# ---------------------------------------------------------------------------- #
