# quanta-materials

Lists and reads project materials from sessions.json and can run jev structured judgments.

## Files

- `mcp_tools.py`

## Requirements

- Python 3.10+
- A Quanta source checkout. This server imports the app's `config` module and reads the app data directory, so it is meant to run alongside the Quanta app.

## Run

```bash
python3 mcp_tools.py
```

Protocol: stdio JSON-RPC, one JSON object per line. stdout must stay JSON-only.

Part of the [Quanta](https://github.com/xxx490202/quanta) ecosystem.
