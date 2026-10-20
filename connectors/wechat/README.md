# Quanta WeChat connector

Connects a personal WeChat account through `weixin-agent-sdk` and forwards each message to Quanta's agent over the app's Unix socket.

## Install

```bash
npm install
```

## Login

```bash
node index.mjs --login
```

Scan the QR code once; the login state is stored by the SDK in `~/.openclaw/`.

## Run

```bash
node index.mjs
```

Part of the [Quanta](https://github.com/xxx490202/quanta) ecosystem.
