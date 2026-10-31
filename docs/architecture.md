# Architecture

This repository's architecture lives in [`ARCHITECTURE.md`](../ARCHITECTURE.md): the module table with
each module's responsibility and allowed dependencies, the harness adapter contract, the recorded architectural
compromises with their trade-offs, and the notes on prompt-tuning boundaries, documentation discovery,
improvement recording and dashboard project documents.

Two reference documents describe the contracts other modules and people depend on:
[dashboard-api.md](dashboard-api.md) for the dashboard's HTTP surface and [improvements-log.md](improvements-log.md)
for the format of `improvements.jsonl`. The run directory contract is section 7 of [spec.md](spec.md).

Record any new module, responsibility change or architectural compromise and its trade-off in
`ARCHITECTURE.md`, as `AGENTS.md` requires. This file deliberately holds no architecture content of its
own, so it cannot drift from that record.
