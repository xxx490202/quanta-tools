#!/usr/bin/env node
// 虚拟 AI 光标的「事件源」：cua_mcp.mjs 动手时调这里，把「AI 在哪儿点」写进一个文件，
// 由 Electron 主进程画成一层点击穿透的浮层（真实指针一点不动）。
//
// 为什么单独一个文件：这套逻辑要能脱开 MCP 单独测（喂个假 driver 就行），
// 而且打包时它跟着 cua_mcp.mjs 一起进 Resources/py（见 electron/tools/pack.js）。
//
// 规矩（照 cc-haha 那套的设计）：
//   · 目标窗口被别的窗口**盖住**时不画——画在挡着它的窗口上等于指着不相干的地方；
//   · 只看不动的工具（截图那几个）先发一条 hide：别让自己的假光标进截图；
//   · 窗口列表探测失败就照常画（这纯是给人看的提示，fail-open）。
import { appendFileSync, existsSync, statSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

const CURSOR_MAX_BYTES = 256 * 1024      // 事件文件超了就从这一条重开（只看最近的）
const CACHE_MS = 400                     // 窗口列表缓存（一次动作里要查好几下）

export function createCursorFeed({ driver, dataDir, log = () => {}, enabled = () => true }) {
  const path = join(dataDir, 'cua-cursor.jsonl')
  let seq = 0
  let last = null                        // 上一次画的点（打字/按键这种没坐标的动作用它）
  let cache = { at: 0, windows: [] }

  async function windowList() {
    const now = Date.now()
    if (now - cache.at < CACHE_MS) return cache.windows
    try {
      const out = await driver.callTool('list_windows', JSON.stringify({ on_screen_only: true }),
                                        { signal: new AbortController().signal })
      const raw = JSON.parse(out.rawJson || '{}')
      cache = { at: now, windows: (raw.structuredContent || {}).windows || [] }
    } catch (err) {
      cache = { at: now, windows: [] }
    }
    return cache.windows
  }

  function inside(bounds, x, y) {
    if (!bounds) return false
    return x >= bounds.x && x <= bounds.x + bounds.width
      && y >= bounds.y && y <= bounds.y + bounds.height
  }

  /** 这个点暴露着吗：从最前面往后找第一个盖住它的普通窗口（layer 0），看是谁的。
   *  没有窗口盖着（点在桌面上）算暴露；查不到窗口列表也算暴露（fail-open）。 */
  async function exposureAt(x, y, pid) {
    if (!Number.isFinite(x) || !Number.isFinite(y)) return { visible: true, app: '' }
    const covering = (await windowList())
      .filter((w) => w.layer === 0 && inside(w.bounds, x, y))
      .sort((a, b) => (b.z_index || 0) - (a.z_index || 0))
    const top = covering[0]
    if (!top) return { visible: true, app: '' }
    if (pid && top.pid !== pid) return { visible: false, app: top.app_name || '' }
    return { visible: true, app: top.app_name || '', pid: top.pid }
  }

  function write(event) {
    try {
      const line = JSON.stringify(event) + '\n'
      try {
        if (existsSync(path) && statSync(path).size > CURSOR_MAX_BYTES) {
          writeFileSync(path, line)
          return
        }
      } catch (err) { /* 量不了就当不用轮转 */ }
      appendFileSync(path, line)
    } catch (err) {
      log('写光标事件失败：' + (err?.message || err))
    }
  }

  /** 动手了：写一条（x/y 缺省就用上一次那个点）。 */
  async function emit(kind, args, extra = {}) {
    if (!enabled()) return null
    const a = args || {}
    let point = null
    if (Number.isFinite(a.x) && Number.isFinite(a.y)) point = { x: a.x, y: a.y }
    else if (kind === 'drag' && Number.isFinite(a.to_x) && Number.isFinite(a.to_y)) {
      // 拖拽：画的落点是**终点**，起点塞进 from（浮层沿这条线拉一道尾迹）
      point = { x: a.to_x, y: a.to_y }
      if (Number.isFinite(a.from_x) && Number.isFinite(a.from_y)) {
        extra = { ...extra, from: [a.from_x, a.from_y] }
      }
    } else if (last) point = { x: last.x, y: last.y }
    const pid = Number.isFinite(a.pid) ? a.pid : 0
    const exposed = await exposureAt(point?.x, point?.y, pid)
    const event = { seq: ++seq, kind, at: Date.now() / 1000,
                    x: point?.x ?? null, y: point?.y ?? null,
                    visible: !!(point && exposed.visible), app: exposed.app || '',
                    pid: exposed.pid || pid || 0, ...extra }
    if (event.visible) last = { x: event.x, y: event.y }
    write(event)
    return event
  }

  /** 截图之前先把自己的光标藏掉（不然它进截图，模型看到的就是自己的假光标）。 */
  function hide() {
    if (!enabled()) return
    write({ seq: ++seq, kind: 'hide', at: Date.now() / 1000, visible: false })
  }

  /** 起落应用 / 激活窗口：把光标滑到那个窗口中间（看得见它跳到哪儿去了）。 */
  async function emitForWindow(kind, args) {
    if (!enabled()) return null
    const a = args || {}
    const windows = await windowList()
    const target = windows.find((w) => (a.window_id && w.window_id === a.window_id)
      || (a.pid && w.pid === a.pid && w.is_on_screen !== false))
    if (!target) return null
    const point = { x: target.bounds.x + target.bounds.width / 2,
                    y: target.bounds.y + Math.min(Math.max(0, target.bounds.height - 40), 80) }
    const exposed = await exposureAt(point.x, point.y, target.pid)
    if (exposed.visible) last = point
    const event = { seq: ++seq, kind, at: Date.now() / 1000,
                    x: point.x, y: point.y, visible: exposed.visible,
                    app: target.app_name || '', pid: target.pid || 0 }
    write(event)
    return event
  }

  return { emit, hide, emitForWindow, path, exposureAt, windowList }
}
