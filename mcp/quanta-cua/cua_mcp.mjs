#!/usr/bin/env node
// 电脑控制（Computer Use）的 MCP server：把 @trycua/cua-driver（Cua Driver，MIT）那套
// 桌面能力挂给 **Claude Code / Codex**。
//
// 为什么是这条路：dsh（DeepSeek 主力）本来就带官方那套 computer use，用的就是这个驱动
// （`@deepseek-ai/dsh-experimental-computer-use-cua-driver-native` 把它的工具目录原样挂出来）。
// 另外两家手上原来只有「只能看」的 screen_capture——这里把同一个驱动的工具挂给它们，
// 挑一个**够用又不淹没上下文**的子集，再给危险动作加一道「问用户」的闸。
//
// 协议：stdio JSON-RPC（和别的 sidecar 一个写法）。stdout 只能出现 JSON-RPC，
// 调试信息一律走 stderr。
//
// 环境变量（桥/主进程挂这个 server 时给）：
//   QUANTA_DATA_DIR   数据目录（screenshots / ask / cua.json 都在它下面）
//   QUANTA_CUA_ROOT   驱动装在哪儿（这个目录下的 node_modules 里有 @trycua/cua-driver）

import { existsSync, mkdirSync, readFileSync, writeFileSync, unlinkSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { pathToFileURL, fileURLToPath } from 'node:url'
import { createInterface } from 'node:readline'
import { homedir } from 'node:os'

// 光标事件（虚拟 AI 光标）那一套单独一个文件：这里 import 进来用，也好单测
const { createCursorFeed } = await import(
  pathToFileURL(join(dirname(fileURLToPath(import.meta.url)), 'cursor_feed.mjs')).href)

const PROTOCOL_FALLBACK = '2025-06-18'
const DATA_DIR = process.env.QUANTA_DATA_DIR || join(homedir(), 'Library', 'Application Support', 'Quanta')
const SHOTS_DIR = join(DATA_DIR, 'screenshots')
const ASK_DIR = join(DATA_DIR, 'ask')
const CONFIG_PATH = join(DATA_DIR, 'cua.json')
// 虚拟 AI 光标：动手的时候往 <数据目录>/cua-cursor.jsonl 追加一行（主进程盯着它 →
// 点击穿透的浮层上动画）。真实指针**永远不动**（输入是按 pid 定向投递的），
// 这只是给人看的「AI 正在哪儿点」。事件的生成/遮挡判定见 cursor_feed.mjs
const ASK_TIMEOUT_MS = 3 * 60 * 1000          // 等用户点头最多 3 分钟
const MAX_IMAGE_BYTES = 3_500_000             // 一张截图别超过这个（超了只落盘、不进上下文）

// ── 挂哪些工具 ────────────────────────────────────────────────────
// 驱动一共 55 个（含它自己那套浏览器、录屏、会话管理、光标样式……）。这里只挂
// 「看 + 动 + 应用起落 + 剪贴板」这一小撮：别的要么本应用已经有更好的（browser_*），
// 要么是给开发调试用的。少而准，模型才不容易用错。
const LOOK_TOOLS = [
  'list_apps', 'list_windows', 'get_window_state', 'verify_state',
  'get_desktop_state', 'check_permissions',
]
const ACT_TOOLS = [
  'launch_app', 'bring_to_front', 'invoke_menu', 'set_window_frame',
  'click', 'double_click', 'right_click', 'drag',
  'type_text', 'press_key', 'hotkey', 'set_value', 'scroll',
  'clipboard_write',
]
// 这几个**每次都要用户点头**：退出/注销/关机、清倒废纸篓、强杀进程、读剪贴板正文
const GATED_TOOLS = ['kill_app', 'clipboard_read']
const ALLOWED = new Set([...LOOK_TOOLS, ...ACT_TOOLS, ...GATED_TOOLS])

/** 硬拦的快捷键：一按就是退出应用 / 注销 / 锁屏 / 强制退出 / 清废纸篓这种不可逆的事。
 *
 *  和「弹卡问用户」是两回事——这些**连问都不问**：用户点「允许」也救不回已经关掉的
 *  未保存文档，而且模型多半能用更好的办法（要退应用就走应用菜单 invoke_menu，那一条
 *  精确到菜单项、也看得见）。cc-haha 那套也是这么分的：确定性黑名单 + 其余交给人。
 */
const BLOCKED_KEYS = [
  ['cmd', 'q'],                 // 退出当前应用
  ['cmd', 'shift', 'q'],        // 注销
  ['cmd', 'ctrl', 'q'],         // 锁屏
  ['cmd', 'opt', 'shift', 'q'], // 关机
  ['cmd', 'opt', 'escape'],     // 强制退出
  ['cmd', 'shift', 'delete'],   // 清倒废纸篓（⌫ 的两种写法都收）
  ['cmd', 'shift', 'backspace'],
]
// 修饰键/键名的各种写法 → 规范名（防 "command+q"、"⌘Q"、"meta+q" 绕过去）
const KEY_ALIASES = {
  cmd: 'cmd', command: 'cmd', meta: 'cmd', '⌘': 'cmd', super: 'cmd',
  opt: 'opt', option: 'opt', alt: 'opt', '⌥': 'opt',
  ctrl: 'ctrl', control: 'ctrl', '⌃': 'ctrl',
  shift: 'shift', '⇧': 'shift',
  escape: 'escape', esc: 'escape', '⎋': 'escape',
  delete: 'delete', backspace: 'delete', del: 'delete', '⌫': 'delete',
}

function normalizeCombo(raw) {
  const out = new Set()
  for (const item of raw || []) {
    const key = String(item ?? '').trim().toLowerCase()
    if (!key) continue
    const mapped = KEY_ALIASES[key]
    if (mapped) out.add(mapped)
    else out.add(key.replace(/^f(\d+)$/, 'f$1'))
  }
  return [...out].sort()
}

/** 这次的按键是不是那条硬拦线（返回一句人话说明，空 = 不在线上）。 */
function whyBlocked(name, args) {
  if (name !== 'hotkey' && name !== 'press_key') return ''
  const raw = name === 'press_key' ? [args.key, ...(args.modifiers || [])] : (args.keys || [])
  const keys = normalizeCombo(raw)
  for (const combo of BLOCKED_KEYS) {
    const want = [...combo].sort()
    if (want.length === keys.length && want.every((k, i) => k === keys[i])) {
      return `按 ${raw.join('+')}（退出应用 / 注销 / 锁屏 / 强制退出 / 清废纸篓这一档）`
    }
  }
  return ''
}

/** 这次调用要不要先问用户（返回一句人话说明，空 = 不用问）。 */
function whyDanger(name, args) {
  if (name === 'kill_app') {
    return `强杀进程 pid=${args.pid}（等于 kill -9，没保存的东西会丢）`
  }
  if (name === 'clipboard_read' && args.include_text) {
    return '读剪贴板里的**正文**（可能正是刚复制的密码、验证码、私信）'
  }
  return ''
}

/** 当前策略：<数据目录>/cua.json，缺省全开（写坏了也当全开，别把功能弄哑）。 */
function policy() {
  const out = { enabled: true, control: true, askDanger: true, cursor: true }
  try {
    const raw = JSON.parse(readFileSync(CONFIG_PATH, 'utf8'))
    for (const key of Object.keys(out)) {
      if (typeof raw?.[key] === 'boolean') out[key] = raw[key]
    }
  } catch (err) { /* 没有那个文件 = 默认 */ }
  return out
}

// ── 虚拟 AI 光标：把「AI 正在哪儿动手」写到文件里（主进程画成点击穿透的浮层）──
// 具体逻辑和遮挡判定在 cursor_feed.mjs（单独一个文件，能脱开 MCP 单测）；
// 这里只把 driver / 数据目录 / 策略开关（cua.json 的 cursor）接上。
let cursorFeed = null

function feed() {
  if (!cursorFeed) {
    cursorFeed = createCursorFeed({ driver, dataDir: DATA_DIR, log,
                                    enabled: () => policy().cursor })
  }
  return cursorFeed
}

// ── 加载驱动 ─────────────────────────────────────────────────────
function driverRoot() {
  if (process.env.QUANTA_CUA_ROOT) return process.env.QUANTA_CUA_ROOT
  const own = join(DATA_DIR, 'cua')
  if (existsSync(join(own, 'node_modules', '@trycua', 'cua-driver'))) return own
  return ''                                   // 主进程会先查，查不到就不会挂这个 server
}

let driver = null
let catalog = new Map()                        // 工具名 → 驱动给的 definition
let loadError = ''

async function load() {
  const root = driverRoot()
  if (!root) { loadError = `找不到 @trycua/cua-driver（看 ${join(DATA_DIR, 'cua')}）`; return }
  const entry = join(root, 'node_modules', '@trycua', 'cua-driver', 'dist', 'index.js')
  if (!existsSync(entry)) { loadError = `驱动的入口不在：${entry}`; return }
  try {
    const mod = await import(pathToFileURL(entry).href)
    driver = mod.CuaDriver.create(undefined)   // 同进程，没有守护进程
    const ac = new AbortController()
    const listed = JSON.parse(await driver.listToolsJson({ signal: ac.signal }))
    for (const tool of listed.tools || []) {
      if (ALLOWED.has(tool.name)) catalog.set(tool.name, tool)
    }
    log(`驱动就绪：${catalog.size} 个工具（从 ${root}）`)
  } catch (err) {
    loadError = String(err?.stack || err?.message || err)
    log('驱动加载失败：' + loadError)
  }
}

// ── 工具结果 → MCP 结果 ───────────────────────────────────────────
// 驱动自己的返回就是 MCP 形状（content + structuredContent），rawJson 里那份最全。
// 图顺手落一份到磁盘：有的运行时看不了内嵌图，给个路径它还能 Read / view_image。
let shotSeq = 0

function toMcpResult(out, name) {
  let parsed = null
  if (typeof out?.rawJson === 'string' && out.rawJson) {
    try { parsed = JSON.parse(out.rawJson) } catch (err) { parsed = null }
  }
  const content = []
  if (Array.isArray(parsed?.content) && parsed.content.length) content.push(...parsed.content)
  else {
    if (out?.text) content.push({ type: 'text', text: String(out.text) })
    for (const image of out?.images || []) {
      if (image?.data) content.push({ type: 'image', mimeType: image.mimeType || 'image/png', data: image.data })
    }
  }
  const structured = parsed?.structuredContent || out?.structuredJson || null
  // 有的工具（list_windows 就是）**详细数据只在 structuredContent 里**，content 只有一句
  // 「Found 13 window(s)」——有的运行时（实测 Codex）不认 structuredContent，模型就只看到
  // 那一句、等于白问。这种「文本很短、又有结构化内容」的，把结构化那份也铺成文本给它。
  const textLen = content.filter((b) => b.type === 'text').reduce((n, b) => n + String(b.text || '').length, 0)
  const hasImage = content.some((b) => b.type === 'image')
  if (structured && !hasImage && textLen < 300) {
    let dump = ''
    try { dump = JSON.stringify(structured) } catch (err) { dump = '' }   // 紧凑一行：模型读 JSON 没问题，省一半字
    if (dump && dump !== '{}') {
      if (dump.length > 20000) dump = dump.slice(0, 20000) + '\n…（截断）'
      content.push({ type: 'text', text: '详细数据：\n' + dump })
    }
  }
  const paths = []
  for (const block of content) {
    if (block.type !== 'image' || !block.data) continue
    try {
      mkdirSync(SHOTS_DIR, { recursive: true })
      shotSeq += 1
      const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)
      const path = join(SHOTS_DIR, `cua-${stamp}-${shotSeq}.png`)
      const bytes = Buffer.from(block.data, 'base64')
      writeFileSync(path, bytes)
      paths.push({ path, bytes: bytes.length })
      if (bytes.length > MAX_IMAGE_BYTES) {
        // 太大了：不进上下文（一张 5K 全屏能有两三 MB），只报路径
        delete block.data
        block.type = 'text'
        block.text = `（这张图太大，没有内嵌；图存在本机：${path}）`
      }
    } catch (err) { log('存截图失败：' + (err?.message || err)) }
  }
  if (paths.length) {
    const first = paths[0]
    if (first.path && content.some((b) => b.type === 'image')) {
      content.push({ type: 'text', text: `（图也在本机：${paths.map((p) => p.path).join('、')}）` })
    }
  }
  if (out?.isError && !content.some((b) => b.type === 'text')) {
    content.push({ type: 'text', text: String(out?.errorCode || '驱动那边报错了') })
  }
  if (!content.length) content.push({ type: 'text', text: '（驱动没有返回内容）' })
  const result = { content, isError: !!out?.isError }
  if (structured) result.structuredContent = structured      // 认这个字段的运行时直接用
  return result
}

// ── 「危险操作先问用户」：走应用那套反问卡（<数据目录>/ask/*.json）─────────
// 和 ask_tools.py 同一个协议：写问题文件 → 桥扫到 → 界面弹卡 → 用户点完写 answer 文件。
// 用 await 睡（别用 Atomics 那种同步等待）：等的时候这条 MCP 连接还能回别的消息。
async function askUser(question, yes = '允许', no = '不允许') {
  const folder = ASK_DIR
  const stamp = `ask-${Date.now()}${Math.floor(Math.random() * 1000)}`
  const questionFile = join(folder, `${stamp}.json`)
  const answerFile = join(folder, `${stamp}.answer.json`)
  try {
    mkdirSync(folder, { recursive: true })
    writeFileSync(questionFile, JSON.stringify({
      id: stamp, at: Date.now() / 1000,
      questions: [{
        header: '电脑控制',
        question,
        options: [
          { label: yes, description: '就这一次；它接着往下做' },
          { label: no, description: '让它停手，改用别的方式' },
        ],
      }],
    }), 'utf8')
  } catch (err) {
    return { ok: false, answer: `问不了用户（${err?.message || err}）：那就别做这一步，跟用户说一声。` }
  }
  const deadline = Date.now() + ASK_TIMEOUT_MS
  try {
    while (Date.now() < deadline) {
      if (existsSync(answerFile)) {
        let data = null
        try { data = JSON.parse(readFileSync(answerFile, 'utf8')) } catch (err) { data = null }
        if (!data) { await sleep(300); continue }
        const picked = (data.answers || [])[0]
        const text = Array.isArray(picked) ? picked.join('、')
          : (typeof picked === 'object' && picked ? String(picked.text || '') : String(picked || ''))
        if (data.dismissed || !text) {
          return { ok: false, answer: '用户把这张卡跳过了：这一步别做，问他一句要不要继续。' }
        }
        if (text.trim() === no || /不|别|停|取消|no/i.test(text)) {
          return { ok: false, answer: `用户选了「${text}」：这一步别做（也别换个说法再试），改用别的方式或者停下来问他。` }
        }
        return { ok: true, answer: `用户选了「${text}」：可以继续。` }
      }
      await sleep(300)
    }
    return { ok: false, answer: '等了三分钟用户也没点：这一步别做，跟他说一声你在等什么。' }
  } finally {
    for (const path of [questionFile, answerFile]) {
      try { unlinkSync(path) } catch (err) { /* 已经被清掉了 */ }
    }
  }
}

function sleep(ms) { return new Promise((done) => setTimeout(done, ms)) }

// ── 调一次工具 ───────────────────────────────────────────────────
async function callTool(name, args) {
  if (!driver) {
    return { content: [{ type: 'text', text: `电脑控制没起来：${loadError || '驱动没加载'}` }], isError: true }
  }
  const cfg = policy()
  if (!cfg.enabled) {
    return { content: [{ type: 'text', text: `电脑控制被关掉了（${CONFIG_PATH} 里 enabled=false）。` }], isError: true }
  }
  const act = ACT_TOOLS.includes(name) || GATED_TOOLS.includes(name)
  if (act && !cfg.control) {
    return { content: [{ type: 'text', text: `现在是「只看不动」档（${CONFIG_PATH} 里 control=false）：这一步动不了，只能看。` }], isError: true }
  }
  const blocked = whyBlocked(name, args || {})
  if (blocked) {
    return { content: [{ type: 'text', text:
      `这一步被硬拦了：${blocked}。这不是「问用户要不要」，是根本不做——`
      + `一按下去就是不可逆的事，用户点允许也救不回来。换个办法：要退应用 / 关窗口就用 `
      + `invoke_menu 走应用菜单（精确到菜单项），或者请用户自己按。别换个写法再试。` }], isError: true }
  }
  const danger = whyDanger(name, args || {})
  if (danger && cfg.askDanger) {
    const asked = await askUser(`这一下要动你的电脑：${danger}。允许吗？`)
    if (!asked.ok) {
      return { content: [{ type: 'text', text: asked.answer }], isError: true }
    }
    log('用户放行：' + danger)
  }
  try {
    // 虚拟光标：截图那几个先藏（别画进图里）；动手的几个在动作前后画/滑
    if (name === 'get_window_state' || name === 'get_desktop_state' || name === 'verify_state') {
      feed().hide()
    }
    const out = await driver.callTool(name, JSON.stringify(args || {}), { signal: new AbortController().signal })
    const result = toMcpResult(out)
    try {
      if (!result.isError) {
        if (name === 'click' || name === 'double_click' || name === 'right_click') {
          await feed().emit('click', args || {}, { button: args?.button || 'left',
                                                   count: name === 'double_click' ? 2 : 1 })
        } else if (name === 'drag') {
          await feed().emit('move', { x: args?.from_x, y: args?.from_y }, {})
          await feed().emit('drag', args || {}, {})
        } else if (name === 'scroll') {
          await feed().emit('scroll', args || {}, { direction: args?.direction || '' })
        } else if (name === 'type_text' || name === 'set_value') {
          await feed().emit('type', args || {}, {})
        } else if (name === 'press_key' || name === 'hotkey') {
          await feed().emit('key', args || {}, {})
        } else if (name === 'launch_app' || name === 'bring_to_front'
                   || name === 'invoke_menu' || name === 'set_window_frame') {
          await feed().emitForWindow('focus', args || {})
        }
      }
    } catch (err) { log('光标事件没发出去（不影响动作）：' + (err?.message || err)) }
    return result
  } catch (err) {
    return { content: [{ type: 'text', text: `这一步没成：${err?.message || err}` }], isError: true }
  }
}

// ── MCP 循环 ─────────────────────────────────────────────────────
function log(text) { process.stderr.write(`[cua] ${text}\n`) }
function send(payload) { process.stdout.write(JSON.stringify(payload) + '\n') }

function toolsList() {
  const tools = []
  for (const name of [...LOOK_TOOLS, ...ACT_TOOLS, ...GATED_TOOLS]) {
    const tool = catalog.get(name)
    if (!tool) continue
    const prefix = GATED_TOOLS.includes(name)
      ? '【本机桌面 · 每次都要用户点头】'
      : (name === 'hotkey' || name === 'press_key'
         ? '【本机桌面 · 会动手（退出/注销/锁屏/清废纸篓这一档的组合是硬拦的）】'
         : (LOOK_TOOLS.includes(name) ? '【本机桌面 · 只看】' : '【本机桌面 · 会动手】'))
    const schema = tool.inputSchema || { type: 'object', properties: {} }
    tools.push({ name, description: `${prefix} ${tool.description || ''}`.slice(0, 6000),
                 inputSchema: schema })
  }
  return tools
}

async function serve() {
  const rl = createInterface({ input: process.stdin })
  for await (const line of rl) {
    const text = String(line).trim()
    if (!text) continue
    let message = null
    try { message = JSON.parse(text) } catch (err) { continue }
    const { method, id } = message
    const params = message.params || {}
    if (method === 'initialize') {
      send({ jsonrpc: '2.0', id, result: {
        protocolVersion: params.protocolVersion || PROTOCOL_FALLBACK,
        capabilities: { tools: { listChanged: false } },
        serverInfo: { name: 'quanta-cua', version: '1.0.0' },
      } })
    } else if (method === 'notifications/initialized' || method === 'notifications/cancelled') {
      continue
    } else if (method === 'ping') {
      send({ jsonrpc: '2.0', id, result: {} })
    } else if (method === 'tools/list') {
      send({ jsonrpc: '2.0', id, result: { tools: toolsList() } })
    } else if (method === 'tools/call') {
      const name = String(params.name || '')
      if (!ALLOWED.has(name)) {
        send({ jsonrpc: '2.0', id, result: { content: [{ type: 'text', text: `没有这个工具：${name}` }], isError: true } })
        continue
      }
      const result = await callTool(name, params.arguments || {})
      send({ jsonrpc: '2.0', id, result })
    } else if (id !== undefined && id !== null) {
      send({ jsonrpc: '2.0', id, error: { code: -32603, message: `不支持的方法：${method}` } })
    }
  }
}

await load()
log(`quanta-cua 已启动（数据目录 ${DATA_DIR}，工具 ${catalog.size} 个）`)
await serve()
if (driver?.shutdown) { try { await driver.shutdown() } catch (err) { /* 收尾而已 */ } }
