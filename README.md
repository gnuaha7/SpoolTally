# SpoolTally

SpoolTally is planned as a local-first service for tracking 3D printer filament spools. The project aims to provide an inventory API, a command-line interface, and a desktop web client while keeping inventory data under the operator's control.

## Current status

This repository contains setup documentation and configuration only. It does not contain an application, an installable package, or runnable SpoolTally commands. The interfaces described below are planned and are not available yet.

The planned Python package is `spooltally`. Its intended commands are `python -m spooltally init`, `python -m spooltally tokens create`, and `python -m spooltally serve`. The planned API uses `/api/v1`. These names describe the project direction; they do not work in this checkout.

## Development

No application setup or test command exists yet. When application code and its approved behavior specifications are added, their owning task will document the supported setup and verification commands here.

See [AGENTS.md](AGENTS.md) for contribution and review rules.
