# Overview

A collection of Pytest-based tests for Charm Integration Testing, focusing on validating the
deployment and interoperability of charms.

## Getting Started

Python dependencies are managed through poetry.

```bash
pipx install poetry==1.6
poetry install
```

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for more information about development best practices.

## Usage

```bash
./scripts/run-test-suite.sh --model {juju_model} --requirer {application}:{endpoint} --provider {application}:{endpoint}
```
