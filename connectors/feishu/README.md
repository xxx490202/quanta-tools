# Quanta Feishu connector

Feishu (Lark) self-built-app long-connection connector. It receives messages through the Feishu WebSocket long connection and sends replies through the OpenAPI. Quanta supplies the app_id/app_secret and turns each message into an agent turn.

## Files

- `feishu.py`
- `im_common.py` — shared IM helpers (dedupe, chunking, attachment download)

## Requirements

- Python 3.10+
- `lark-oapi`
- A Quanta source checkout; the module uses the app's `config` for the data directory

## Run

This module is started by Quanta's bridge, not as a standalone daemon. See [Quanta](https://github.com/xxx490202/quanta).
