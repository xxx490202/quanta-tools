// 微信连接器（个人号）—— 用 weixin-agent-sdk 那条路：
//   一次扫码登录（登录状态它自己存在 ~/.openclaw/），之后长轮询收消息，不用公网服务器。
// 每条消息交给应用（Quanta）里同一个 agent 跑一轮，回答发回微信。
//
// 跟应用怎么联系：走应用那条现成的 unix socket（quanta-cli.sock），问一句、等回答。
//   {token: <quanta-cli.json 里的>， cmd: 'ask', page: 'project',
//    channel: 'wechat:<会话 id>', message: '...'}
//   应用会像界面里那样跑一轮，把事件镜像回来；这里只挑 reply 那条、拿正文。
//
// 用法：
//   node index.mjs --login     # 只做扫码登录：把二维码**链接**报给应用（应用画成图），
//                              # 扫完把登录信息按 SDK 的格式写进 ~/.openclaw/
//   node index.mjs             # 起机器人（没登录过会报错退出）
//
// 给应用看的状态（一行一条 JSON，写在 stdout 上）：
//   {"quanta":{"qr":"<二维码里的链接>"}}        # 请应用把这张二维码显示出来
//   {"quanta":{"scan":"scanned"}}               # 已经扫了，等手机上确认
//   {"quanta":{"login":"ok","account":"..."}}   # 登录完成（应用的二维码该收了）
//   {"quanta":{"ready":true}}                   # 机器人跑起来了
//   {"quanta":{"error":"..."}}                  # 出错了

import fs from 'node:fs'
import net from 'node:net'
import os from 'node:os'
import path from 'node:path'
import process from 'node:process'

const DATA_DIR = process.env.QUANTA_DATA_DIR
  || path.join(os.homedir(), 'Library', 'Application Support', 'Quantification')

// 二维码/登录这几个接口（照 weixin-agent-sdk 的 auth/login-qr.ts，MIT）
const LOGIN_BASE = 'https://ilinkai.weixin.qq.com'
const BOT_TYPE = '3'
const QR_TIMEOUT_MS = 35_000

function tell(payload) {
  try { process.stdout.write(JSON.stringify({ quanta: payload }) + '\n') } catch { /* 忽略 */ }
}

// ── 登录状态存哪儿（和 SDK 一个地方、一个格式，这样 SDK 的 start() 认得）
function stateDir() {
  return process.env.OPENCLAW_STATE_DIR?.trim()
    || process.env.CLAWDBOT_STATE_DIR?.trim()
    || path.join(os.homedir(), '.openclaw')
}
function weixinDir() { return path.join(stateDir(), 'openclaw-weixin') }
function normalizeAccountId(raw) { return String(raw || '').trim().toLowerCase().replace(/[@.]/g, '-') }
function saveAccount(accountId, data) {
  const dir = path.join(weixinDir(), 'accounts')
  fs.mkdirSync(dir, { recursive: true })
  const file = path.join(dir, `${accountId}.json`)
  let old = {}
  try { old = JSON.parse(fs.readFileSync(file, 'utf8')) } catch { old = {} }
  const token = (data.token || '').trim() || old.token
  const baseUrl = (data.baseUrl || '').trim() || old.baseUrl
  const userId = (data.userId !== undefined ? String(data.userId || '').trim() : old.userId) || undefined
  const out = {
    ...(token ? { token, savedAt: new Date().toISOString() } : {}),
    ...(baseUrl ? { baseUrl } : {}),
    ...(userId ? { userId } : {}),
  }
  fs.writeFileSync(file, JSON.stringify(out, null, 2), 'utf8')
  try { fs.chmodSync(file, 0o600) } catch { /* 尽力而为 */ }
  fs.mkdirSync(weixinDir(), { recursive: true })
  fs.writeFileSync(path.join(weixinDir(), 'accounts.json'), JSON.stringify([accountId], null, 2), 'utf8')
}

async function getJson(url) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), QR_TIMEOUT_MS)
  try {
    const response = await fetch(url, { signal: controller.signal })
    return JSON.parse(await response.text())
  } finally {
    clearTimeout(timer)
  }
}

// ── 扫码登录：拿到二维码链接 → 报给应用 → 长轮询等确认 → 存登录信息
async function qrLogin() {
  let qr = null
  try {
    qr = await getJson(`${LOGIN_BASE}/ilink/bot/get_bot_qrcode?bot_type=${encodeURIComponent(BOT_TYPE)}`)
  } catch (error) {
    tell({ error: '拿二维码失败：' + String(error && error.message || error) })
    return false
  }
  const link = String(qr && qr.qrcode_img_content || '')
  const key = String(qr && qr.qrcode || '')
  if (!link || !key) {
    tell({ error: '二维码没拿到（平台没回内容）' })
    return false
  }
  tell({ qr: link })
  let base = LOGIN_BASE
  let scanned = false
  const deadline = Date.now() + 8 * 60 * 1000
  while (Date.now() < deadline) {
    let status = null
    try {
      status = await getJson(`${base}/ilink/bot/get_qrcode_status?qrcode=${encodeURIComponent(key)}`)
    } catch {
      status = { status: 'wait' }                       // 网关抽风就继续等
    }
    const state = String(status && status.status || 'wait')
    if (state === 'scaned_but_redirect' && status.redirect_host) {
      base = `https://${status.redirect_host}`          // 换了机房：跟着走
      continue
    }
    if (state === 'scaned' && !scanned) {
      scanned = true
      tell({ scan: 'scanned' })                        // 应用可以把二维码底下那句话换掉
      continue
    }
    if (state === 'expired') {
      tell({ error: '二维码过期了：再点一次「登录微信」' })
      return false
    }
    if (state === 'confirmed') {
      const raw = String(status.ilink_bot_id || '')
      if (!raw || !status.bot_token) {
        tell({ error: '登录回来了，但账号信息不全' })
        return false
      }
      const accountId = normalizeAccountId(raw)
      saveAccount(accountId, { token: String(status.bot_token),
                               baseUrl: String(status.baseurl || ''),
                               userId: String(status.ilink_user_id || '') })
      tell({ login: 'ok', account: accountId })
      return true
    }
  }
  tell({ error: '等扫码等超时了：再点一次「登录微信」' })
  return false
}

function cliConfig() {
  return JSON.parse(fs.readFileSync(path.join(DATA_DIR, 'quanta-cli.json'), 'utf8'))
}

// ── 问应用一句话（走 unix socket）─────────────────────────────────────
function askApp(text, conversationId) {
  return new Promise((resolve, reject) => {
    const conf = cliConfig()
    const sock = net.createConnection(conf.socket)
    let buffer = ''
    let reply = ''
    let done = false
    const finish = (error, value) => {
      if (done) return
      done = true
      try { sock.destroy() } catch { /* 忽略 */ }
      error ? reject(error) : resolve(value)
    }
    sock.setTimeout(15 * 60 * 1000)                    // 一轮最多等 15 分钟
    sock.on('timeout', () => finish(new Error('等应用回话超时了')))
    sock.on('error', (error) => finish(error))
    sock.on('connect', () => {
      sock.write(JSON.stringify({
        token: conf.token, cmd: 'ask', page: 'project',
        channel: 'wechat:' + String(conversationId || ''),
        message: String(text || ''),
      }) + '\n')
    })
    sock.on('data', (chunk) => {
      buffer += chunk.toString('utf8')
      let index = buffer.indexOf('\n')
      while (index >= 0) {
        const line = buffer.slice(0, index)
        buffer = buffer.slice(index + 1)
        index = buffer.indexOf('\n')
        if (!line.trim()) continue
        let event = null
        try { event = JSON.parse(line) } catch { continue }
        if (event.type === 'reply') reply = String(event.text || '')
        else if (event.type === 'cli_error') finish(new Error(String(event.text || '应用报错')))
        else if (event.type === 'cli_done') finish(null, reply)
      }
    })
    sock.on('close', () => finish(null, reply))
  })
}

// 账号文件都在 accounts/ 下；索引（accounts.json）要是空了，挑最新的一个补上
function repairIndex() {
  try {
    const dir = path.join(weixinDir(), 'accounts')
    if (!fs.existsSync(dir)) return false
    let best = null
    for (const name of fs.readdirSync(dir)) {
      if (!name.endsWith('.json')) continue
      try {
        const data = JSON.parse(fs.readFileSync(path.join(dir, name), 'utf8'))
        if (!String(data.token || '').trim()) continue
        const at = Date.parse(data.savedAt || '') || 0
        if (!best || at > best.at) best = { id: name.slice(0, -5), at }
      } catch { /* 跳过 */ }
    }
    if (!best) return false
    fs.mkdirSync(weixinDir(), { recursive: true })
    fs.writeFileSync(path.join(weixinDir(), 'accounts.json'), JSON.stringify([best.id], null, 2), 'utf8')
    return true
  } catch {
    return false
  }
}

// ── 起机器人 ────────────────────────────────────────────────────────
async function runBot() {
  let sdk = null
  try {
    sdk = await import('weixin-agent-sdk')
  } catch (error) {
    tell({ error: '没装上 weixin-agent-sdk（在 connectors/wechat 里跑一次 npm install）：'
      + String(error && error.message || error) })
    return false
  }
  if (!sdk.isLoggedIn()) {
    // 索引文件（accounts.json）可能被清空了，但账号文件还在、token 也在——补回来再试。
    // 不补的话 SDK 会一口咬定「没登录」，扫码白扫（用户报过「扫完发消息没反应」）
    const repaired = repairIndex()
    if (!repaired) {
      tell({ error: '还没登录微信：先在设置页点「登录微信」扫一次码' })
      return false
    }
  }
  const agent = {
    async chat(request) {
      const text = String((request && request.text) || '').trim()
      if (!text) return { text: '（这条我只看文字：图片、语音这些还不会读）' }
      const answer = await askApp(text, request.conversationId)
      return { text: answer || '（这一轮没有正文）' }
    },
  }
  const bot = sdk.start(agent)
  tell({ ready: true })
  await bot.wait()
  return true
}

async function logout() {
  try {
    const sdk = await import('weixin-agent-sdk')
    sdk.logout()
    tell({ logout: 'ok' })
    return true
  } catch (error) {
    tell({ error: '退出登录失败：' + String(error && error.message || error) })
    return false
  }
}

async function main() {
  const args = process.argv.slice(2)
  let ok = false
  if (args.includes('--login')) ok = await qrLogin()
  else if (args.includes('--logout')) ok = await logout()
  else ok = await runBot()
  process.exit(ok ? 0 : 1)
}

main().catch((error) => {
  tell({ error: String((error && error.stack) || error) })
  process.exit(1)
})
