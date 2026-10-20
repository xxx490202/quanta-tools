# quanta-ask

Lets the agent ask the user a question through Quanta's selection-card UI.

## Files

- `ask_tools.py`

## Requirements

- Python 3.10+
- A Quanta source checkout. This server imports the app's `config` module and reads the app data directory, so it is meant to run alongside the Quanta app.

## Run

```bash
python3 ask_tools.py
```

Protocol: stdio JSON-RPC, one JSON object per line. stdout must stay JSON-only.

Part of the [Quanta](https://github.com/xxx490202/quanta) ecosystem.
