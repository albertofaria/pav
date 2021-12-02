# ---------------------------------------------------------------------------- #

FROM python:3.9-slim as test

WORKDIR /pav

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY mypy.ini pyproject.toml ./
COPY pav/ pav/
COPY tests/ tests/

RUN black --check .
RUN isort --check .
RUN mypy --no-incremental --cache-dir=/dev/null
RUN PYTHONPATH=. pytest

# ---------------------------------------------------------------------------- #

FROM python:3.9-slim as build

WORKDIR /pav

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pav/ pav/

ENTRYPOINT [ "python", "-m", "pav" ]

# ---------------------------------------------------------------------------- #
