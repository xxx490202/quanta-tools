# quanta-cua

Desktop computer-use MCP server built on @trycua/cua-driver.

## Files

- `cua_mcp.mjs`
- `cursor_feed.mjs`

## Requirements

- Node.js 18+
- `@trycua/cua-driver`
- Environment variables:
  - `QUANTA_DATA_DIR` — defaults to `~/Library/Application Support/Quanta`
  - `QUANTA_CUA_ROOT` — directory whose `node_modules` contains `@trycua/cua-driver`

## Install

```bash
npm install @trycua/cua-driver
```

## Run

```bash
node cua_mcp.mjs
```

Protocol: stdio JSON-RPC, one JSON object per line. stdout must stay JSON-only.

Part of the [Quanta](https://github.com/xxx490202/quanta) ecosystem.
