# Quanta QQ connector

Official QQ bot connector: WebSocket gateway + passive replies.

## Files

- `qq.py`
- `im_common.py` — shared IM helpers (dedupe, chunking, attachment download)

## Requirements

- Python 3.10+
- `websocket-client`
- A Quanta source checkout; the module uses the app's `config` for the data directory

## Run

This module is started by Quanta's bridge, not as a standalone daemon. See [Quanta](https://github.com/xxx490202/quanta).
