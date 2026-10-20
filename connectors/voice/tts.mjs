#!/usr/bin/env node
// TTS 侧车：朗读引擎走 Edge（微软的在线语音，用户点名要的）。
//
//   node tts.mjs --status                    看模型齐不齐（当前引擎 + 每个引擎各一份结论）
//   node tts.mjs --download [--reset]        下当前引擎的模型（按仓库文件树并发下，可断点续传）
//   node tts.mjs --engine piper --say "你好" --out x.wav    合成一句
//   node tts.mjs --serve                     常驻：stdin 一行一条 JSON {id,text,voice,speed}，
//                                            stderr 回 [voice] {"event":"audio","id":1,"wav":"<base64>"}
//
// 引擎用环境变量 QUANTA_TTS_ENGINE 选（界面上的 pref 由桥传进来）。
//
// 结构上还是「多引擎」的架子（ENGINES 表 + --engine / QUANTA_TTS_ENGINE 选）：
// 以后要加本地的（Piper/Kokoro 那些）或者别的在线服务，加一块表就行，代码不用动。
import { randomBytes } from 'node:crypto'
import { createReadStream, createWriteStream } from 'node:fs'
import { mkdir, rename, stat, unlink, writeFile, readFile, readdir, copyFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { createInterface } from 'node:readline'
import os from 'node:os'
import path from 'node:path'
import { Readable } from 'node:stream'
import { pipeline } from 'node:stream/promises'
import { TransformStream } from 'node:stream/web'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))

// 国内拉 huggingface 常常连不上：同一个路径换到 hf-mirror（官方镜像）再试一次
const MIRRORS = [null, 'hf-mirror.com']

// ── 引擎表 ──────────────────────────────────────────────────
// assets 不用手写清单：下的时候按仓库的文件树来（列在 build 里的路径就是全部要的东西）。
// espeak: 'own' 自己那份 espeak-ng-data；'kokoro' 借 kokoro 那份（中文语音要用 cmn_dict，
// 下载时把自己的 cmn_dict 拷过去）。
const ENGINES = {
}

ENGINES.edge = {
  label: 'Edge 语音（联网）',
  repo: '',                        // 不用下模型：这是微软 Edge 的在线语音
  dir: '', network: true,
  // 只留这五个（晓北 / 晓妮 / 云健 用户不要，删了）
  voices: [
    ['zh-CN-XiaoxiaoNeural', 0, '晓晓', 'Xiaoxiao'],
    ['zh-CN-XiaoyiNeural', 1, '晓伊', 'Xiaoyi'],
    ['zh-CN-YunxiNeural', 2, '云希', 'Yunxi'],
    ['zh-CN-YunyangNeural', 3, '云扬', 'Yunyang'],
    ['zh-CN-YunxiaNeural', 4, '云夏', 'Yunxia'],
  ],
  build() { return null },
}

const ENGINE = (() => {
  const at = process.argv.indexOf('--engine')
  const fromFlag = at >= 0 ? (process.argv[at + 1] || '') : ''
  const want = (fromFlag || process.env.QUANTA_TTS_ENGINE || '').trim()
  return Object.prototype.hasOwnProperty.call(ENGINES, want) ? want : 'edge'
})()

// 这个文件要不要下：借别人 espeak 的引擎只需要自己那份 cmn_dict（其余用 kokoro 那份）
function skipPath(engine, entryPath) {
  if (SKIP_RE.test(entryPath)) return true
  const cfg = ENGINES[engine]
  if (cfg.espeak && cfg.espeak !== 'own' && entryPath.startsWith('espeak-ng-data/')
      && !entryPath.endsWith('cmn_dict')) return true
  return false
}

function dataDir() {
  const env = (process.env.QUANTA_DATA_DIR || '').trim()
  if (env) return env
  return path.join(os.homedir(), 'Library', 'Application Support', 'Quantification')
}

function engineDir(engine) {
  const env = (process.env.QUANTA_TTS_MODELS || '').trim()
  if (env && engine === ENGINE) return env
  return path.join(dataDir(), 'voice', ENGINES[engine].dir)
}

function modelDir(engine = ENGINE) {
  return engineDir(engine)
}

// 引擎：优先用这条侧车自己的 node_modules（打包时带上），否则用 dsh 那份
function requireEngine() {
  const require = createRequire(import.meta.url)
  const roots = [
    (process.env.QUANTA_VOICE_ENGINE || '').trim(),
    path.join(HERE, 'node_modules'),
    path.join(dataDir(), 'dsh', 'node_modules'),
  ].filter(Boolean)
  for (const root of roots) {
    try {
      return require(require.resolve('sherpa-onnx-node', { paths: [root] }))
    } catch (error) { /* 换下一个 */ }
  }
  throw new Error('找不到 sherpa-onnx-node（既没有自带的，也没在 dsh 那份里）')
}

// 进度回报：一行 JSON（桥那边照着画进度条）
function report(payload) {
  console.error('[voice] ' + JSON.stringify(payload))
}

// ── 下载锁：同一时间只允许一份下载在跑 ─────────────────────────
function lockFile(engine = ENGINE) {
  return path.join(modelDir(engine), 'download.lock')
}

async function takeLock(engine = ENGINE) {
  try {
    const raw = await readFile(lockFile(engine), 'utf8')
    const holder = Number((raw || '').trim()) || 0
    if (holder && holder !== process.pid) {
      try {
        process.kill(holder, 0)
        return false                    // 那位还活着：让它下
      } catch (error) { /* 不在了，锁可以抢 */ }
    }
  } catch (error) { /* 没有锁文件 */ }
  try {
    await mkdir(modelDir(engine), { recursive: true })
    await writeFile(lockFile(engine), String(process.pid))
  } catch (error) { /* 写不进去也照下（锁是尽力而为） */ }
  return true
}

async function dropLock(engine = ENGINE) {
  await unlink(lockFile(engine)).catch(() => {})
}

// ── 下载 ────────────────────────────────────────────────────
async function fileOk(file, bytes) {
  try {
    const info = await stat(file)
    return info.isFile() && (!bytes || info.size === bytes)
  } catch (error) {
    return false
  }
}

// 仓库文件树（path + size）：清单不手写，直接从 HF 的 API 拉，换模型不用改代码
async function repoTree(repo) {
  for (const mirror of MIRRORS) {
    const api = (mirror ? `https://${mirror}` : 'https://huggingface.co')
      + `/api/models/${repo}/tree/main?recursive=true`
    try {
      const response = await fetch(api, { signal: AbortSignal.timeout(20000) })
      if (!response.ok) continue
      const parsed = await response.json()
      if (Array.isArray(parsed)) return parsed
    } catch (error) { /* 换镜像 */ }
  }
  return null
}

const SKIP_RE = /(^|\/)(\.gitattributes|README\.md|MODEL_CARD|.*\.py|.*\.sh|.*\.onnx\.json)$|\.tar\.bz2$|\.pth$|rule\.far$/i

async function downloadOne(engine, entry, options = {}) {
  const quiet = !!options.quiet
  const want = typeof entry.size === 'number' ? entry.size : Number(entry.bytes) || 0
  const dir = modelDir(engine)
  const file = path.join(dir, entry.path)
  // 稳定的 .part 名字：断了（或镜像挂起被掐掉）能**接着下**，不用重头来
  const part = file + '.part'
  const url = `https://huggingface.co/${ENGINES[engine].repo}/resolve/main/${entry.path}`
  let lastError = null
  for (const mirror of MIRRORS) {
    const target = mirror ? url.replace('huggingface.co', mirror) : url
    let controller = null
    let watchdog = null
    try {
      let offset = 0
      try { offset = (await stat(part)).size } catch (error) { offset = 0 }
      if (want && offset > want) {
        await unlink(part).catch(() => {})
        offset = 0
      }
      if (want && offset === want) {                     // 上次其实下完了，只是没来得及改名
        await rename(part, file)
        return
      }
      controller = new AbortController()
      const response = await fetch(target, { redirect: 'follow',
        headers: offset ? { Range: `bytes=${offset}-` } : {}, signal: controller.signal })
      if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`)
      if (offset && response.status !== 206) {           // 对面不给接着下：这一份从头来
        await unlink(part).catch(() => {})
        offset = 0
      }
      await mkdir(path.dirname(file), { recursive: true })
      let got = offset
      let last = Date.now()
      let lastReport = 0
      // 连接挂起（镜像偶尔会这样）：20 秒没新字节就当它死了，掐掉重试——能续传，代价很小
      watchdog = setInterval(() => {
        if (Date.now() - last > 20000) controller.abort()
      }, 5000)
      const counter = new TransformStream({
        transform(chunk, ctl2) {
          got += chunk.length
          last = Date.now()
          const now = Date.now()
          if (!quiet && now - lastReport > 1000) {
            lastReport = now
            report({ event: 'download', file: entry.path, done: got, total: want,
                     source: mirror || 'huggingface.co', resumed: offset > 0 })
          }
          ctl2.enqueue(chunk)
        },
      })
      await pipeline(Readable.fromWeb(response.body.pipeThrough(counter)),
                     createWriteStream(part, offset ? { flags: 'a' } : {}))
      if (want && got !== want) throw new Error(`拿到 ${got} 字节（应为 ${want}）`)
      await rename(part, file)
      return
    } catch (error) {
      lastError = error
      report({ event: 'note',
               text: `${entry.path} 没成功：${error.message}（已下的部分留着，接着来）` })
    } finally {
      if (watchdog) clearInterval(watchdog)
    }
  }
  throw new Error(`下不动 ${entry.path}：${lastError && lastError.message}`)
}

// 把某个引擎（+ 它依赖的那份 espeak 数据）按文件树补齐
async function ensureEngine(engine = ENGINE, reset = false) {
  const cfg = ENGINES[engine]
  const dir = modelDir(engine)
  if (cfg.network) return dir                  // 联网引擎：没有模型要下
  const jobs = [{ engine, repo: cfg.repo, dir }]
  if (cfg.espeak && cfg.espeak !== 'own') {
    jobs.push({ engine: cfg.espeak, repo: ENGINES[cfg.espeak].repo, dir: modelDir(cfg.espeak) })
  }
  let done = 0
  for (const job of jobs) {
    const tree = await repoTree(job.repo)
    if (!tree) {
      report({ event: 'note', text: `文件清单拉不到（${job.repo}）` })
      continue
    }
    if (reset && job.engine === engine) {
      for (const entry of tree) {
        if (entry.type !== 'file' || skipPath(job.engine, entry.path)) continue
        await unlink(path.join(job.dir, entry.path)).catch(() => {})
      }
      report({ event: 'note', text: '旧的模型文件已清掉' })
    }
    const missing = []
    for (const entry of tree) {
      if (entry.type !== 'file' || !entry.path || typeof entry.size !== 'number') continue
      if (skipPath(job.engine, entry.path)) continue
      if (await fileOk(path.join(job.dir, entry.path), entry.size)) continue
      missing.push(entry)
    }
    if (!missing.length) continue
    report({ event: 'note', text: `${ENGINES[job.engine].label}：要下 ${missing.length} 个文件（并发 8）` })
    let cursor = 0
    let finished = 0
    const total = missing.length
    const worker = async () => {
      while (cursor < total) {
        const entry = missing[cursor]
        cursor += 1
        try {
          await downloadOne(job.engine, entry, { quiet: total > 4 })
        } catch (error) {
          report({ event: 'note', text: `${entry.path} 没下来：${error.message}` })
        }
        finished += 1
        done += 1
        if (total > 4 && (finished % 5 === 0 || finished === total)) {
          report({ event: 'download', file: ENGINES[job.engine].label, done: finished,
                   total, source: 'extras' })
        }
      }
    }
    await Promise.all(Array.from({ length: 8 }, () => worker()))
  }
  // 借 espeak 的引擎：把自己那份 cmn_dict 拷到借来的 espeak-ng-data 里（中文语音要用）
  if (cfg.espeak && cfg.espeak !== 'own') {
    try {
      await copyFile(path.join(modelDir(engine), 'espeak-ng-data', 'cmn_dict'),
                     path.join(modelDir(cfg.espeak), 'espeak-ng-data', 'cmn_dict'))
    } catch (error) { /* 那份没有 cmn_dict 就算了 */ }
  }
  return dir
}

// ── 状态 ────────────────────────────────────────────────────
// ready 的判定：核心文件在不在（够用就行，不做 sha256——文件多、换模型勤）
async function engineReady(engine) {
  const cfg = ENGINES[engine]
  const dir = modelDir(engine)
  const tree = await repoTree(cfg.repo)
  if (!tree) return { ready: false, files: {}, error: '清单拉不到（网络？）' }
  const files = {}
  let ready = true
  for (const entry of tree) {
    if (entry.type !== 'file' || !entry.path || typeof entry.size !== 'number') continue
    if (skipPath(engine, entry.path)) continue
    const ok = await fileOk(path.join(dir, entry.path), entry.size)
    files[entry.path] = ok
    if (!ok) ready = false
  }
  if (cfg.espeak && cfg.espeak !== 'own') {
    const has = await fileOk(path.join(modelDir(cfg.espeak), 'espeak-ng-data', 'cmn_dict'))
    if (!has) ready = false
    files['（借的 espeak 数据）'] = has
  }
  return { ready, files, models: dir }
}

// 每个引擎只查几个**关键文件**：拉整个仓库清单要好几秒，而界面每 10 秒就会问一次状态。
// 少的那几个文件下没下全都由 --download 负责（它按文件树补齐）；这里只判「能不能开工」
const REQUIRED = {
}

async function engineReadyQuick(engine) {
  const cfg = ENGINES[engine]
  if (cfg.network) return true                 // 联网引擎：不用下东西，能连就行（连不上合成时才报错）
  for (const rel of REQUIRED[engine] || []) {
    if (!(await fileOk(path.join(modelDir(engine), rel)))) return false
  }
  if (cfg.espeak && cfg.espeak !== 'own') {
    return fileOk(path.join(modelDir(cfg.espeak), 'espeak-ng-data', 'cmn_dict'))
  }
  return true
}

async function readyInfo() {
  let engineError = ''
  try { requireEngine() } catch (error) { engineError = error.message }
  const current = await engineReadyQuick(ENGINE)
  const engines = {}
  const engineList = []
  for (const key of Object.keys(ENGINES)) {
    const ready = key === ENGINE ? current : await engineReadyQuick(key)
    engines[key] = ready
    engineList.push({ key, label: ENGINES[key].label, ready,
                      voices: ENGINES[key].voices.map(([id, sid, zh, en]) => ({ id, sid, zh, en })) })
  }
  return { engine: ENGINE, label: ENGINES[ENGINE].label, engines, engineList,
           voices: ENGINES[ENGINE].voices.map(([id, sid, zh, en]) => ({ id, sid, zh, en })),
           ready: !engineError && current, engineError, models: modelDir(ENGINE), files: {} }
}

async function status() {
  process.stdout.write(JSON.stringify(await readyInfo()) + '\n')
}

// ── 合成 ────────────────────────────────────────────────────
function voiceSid(engine, voice) {
  const list = ENGINES[engine].voices
  const hit = list.find((row) => row[0] === voice)
  return hit ? hit[1] : list[0][1]
}

// ── Edge 语音（联网）────────────────────────────────────────
// 微软 Edge「朗读」那个在线语音：免费、音质明显好，但**文字会发到微软服务器**（用户点头了）。
// 照 edge-tts 的握手来：TrustedClientToken + Sec-MS-GEC（时间签名）+ 版本号 + muid cookie。
//
// **Sec-MS-GEC 有两代**。2026-10 中旬微软把闸门换到 v2：v2 的令牌是
//   <16位十六进制 CS64>-<64位十六进制「改过的 SHA-256」>
// 而且要配 `Sec-MS-GEC-Version: 2-<版本>`。老的那套（单个 SHA-256、`1-` 前缀）
// 现在**一律 403**，不管版本号填多少——用户 2026-10-18 报「Rhema 有字没声」就是这个：
// Edge 403 → 只剩系统语音兜底。下面这块是从 edge-tts 7.3.1 的 drm.py 逐行搬过来的
// （CS64 是自定义摘要，不是标准哈希，别想着用 crypto 现成的替换）。
const EDGE = {
  token: '6A5AA1D4EAFF4E9FB37E23D68491D6F4',
  version: '143.0.3650.75',
  gecVersion: '2-143.0.3650.75',        // 前缀 2 = v2 令牌（见上）
  winEpoch: 11644473600,
}

// —— Sec-MS-GEC v2（移植自 edge-tts 7.3.1 drm.py）——
const GEC_U32 = 0xFFFFFFFF
const GEC_SHA256_INITIAL = [0x6A09E667, 0xBB67AE85, 0x3C6EF372, 0xA54FF53A,
  0x510E527F, 0x9B05688C, 0x1F83D9AB, 0x5BE0CD19]
const GEC_SHA256_STATE = [0xFD9FBEA2, 0x91243CE7, 0xBE499094, 0xCD2E1E0A,
  0x6C006F49, 0x04BD35AB, 0xFEB0A7E4, 0x03EFFBD2]
const GEC_SCHEDULE = [0xEB24BF9E, 0x1558691F, 0xA0C09EE2, 0x0CFA32BA,
  0xA622EE8C, 0xD1DD9491, 0xC0DD25A0, 0xF9E70AB6, 0xF0C31573, 0x7058CE4E,
  0x7188E606, 0x00E5AB33, 0xD258B060, 0x72766564]
const GEC_K = [
  0x428A2F98, 0x71374491, 0xB5C0FBCF, 0xE9B5DBA5, 0x3956C25B, 0x59F111F1, 0x923F82A4, 0xAB1C5ED5,
  0xD807AA98, 0x12835B01, 0x243185BE, 0x550C7DC3, 0x72BE5D74, 0x80DEB1FE, 0x9BDC06A7, 0xC19BF174,
  0xE49B69C1, 0xEFBE4786, 0x0FC19DC6, 0x240CA1CC, 0x2DE92C6F, 0x4A7484AA, 0x5CB0A9DC, 0x76F988DA,
  0x983E5152, 0xA831C66D, 0xB00327C8, 0xBF597FC7, 0xC6E00BF3, 0xD5A79147, 0x06CA6351, 0x14292967,
  0x27B70A85, 0x2E1B2138, 0x4D2C6DFC, 0x53380D13, 0x650A7354, 0x766A0ABB, 0x81C2C92E, 0x92722C85,
  0xA2BFE8A1, 0xA81A664B, 0xC24B8B70, 0xC76C51A3, 0xD192E819, 0xD6990624, 0xF40E3585, 0x106AA070,
  0x19A4C116, 0x1E376C08, 0x2748774C, 0x34B0BCB5, 0x391C0CB3, 0x4ED8AA4A, 0x5B9CCA4F, 0x682E6FF3,
  0x748F82EE, 0x78A5636F, 0x84C87814, 0x8CC70208, 0x90BEFFFA, 0xA4506CEB, 0xBEF9A3F7, 0xC67178F2]
const GEC_ADJUSTMENT = 0xC9DC06A7
const GEC_BLOCK_K = [...GEC_K.slice(0, 14), GEC_ADJUSTMENT, ...GEC_K.slice(15)]
const GEC_SIGMA0_ADJUSTMENT = 0x059C0B80
const GEC_SIGMA1_ADJUSTMENT = 0x000B92C0
const GEC_COEFFICIENTS = [
  [0xE82E91B1, 0xB32700D0], [0x44DE24C6, 0x88DFF51D], [0x3BEB65BC, 0x29514038],
  [0xC89C5933, 0xB328CBDF], [0x8D1EC211, 0xD405B4D5], [0x314EF5D5, 0x26E55F3C],
  [0xE92491B1, 0x607801C6], [0xF0A5B9D5, 0x82B2C1FB], [0x06064CF3, 0xFA51A3DA],
  [0xDA79FA60, 0x207CC464], [0xBED20560, 0x3C3C74F3], [0x89F9C9F8, 0xD2D14B61],
  [0x35FA38C6, 0xBB04A2C1], [0xCB49E3C6, 0xC5626167],
]
const GEC_FACTOR = 0x82893933
const GEC_ADJUSTMENT0 = 0xF2F90B1A
const GEC_ADJUSTMENT1 = 0x0B1A0000

const rol16 = (v) => (((v << 16) | (v >>> 16)) >>> 0)
const rotr = (v, n) => (((v >>> n) | (v << (32 - n))) >>> 0)
const gecSigma0 = (v) => ((rotr(v, 7) ^ rotr(v, 18) ^ (v >>> 3)) >>> 0)
const gecSigma1 = (v) => ((rotr(v, 17) ^ rotr(v, 19) ^ (v >>> 10)) >>> 0)
const imul32 = (a, b) => (Math.imul(a, b) >>> 0)

function gecRounds(state, words, firstRound = 0) {
  let [a, b, c, d, e, f, g, h] = state
  const K = firstRound === 14 ? GEC_BLOCK_K : GEC_K
  for (let i = firstRound; i < 64; i += 1) {
    const s1 = (rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)) >>> 0
    const ch = (g ^ (e & (f ^ g))) >>> 0
    const t1 = (h + s1 + ch + K[i] + words[i]) >>> 0
    const s0 = (rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)) >>> 0
    const maj = ((a & b) | (c & (a | b))) >>> 0
    const t2 = (s0 + maj) >>> 0
    h = g; g = f; f = e; e = (d + t1) >>> 0; d = c; c = b; b = a; a = (t1 + t2) >>> 0
  }
  return [a, b, c, d, e, f, g, h]
}

function gecExpand(words, firstWord = 16) {
  for (let i = firstWord; i < 64; i += 1) {
    const s1 = rotr(words[i - 2], 17) ^ rotr(words[i - 2], 19) ^ (words[i - 2] >>> 10)
    const s0 = rotr(words[i - 15], 7) ^ rotr(words[i - 15], 18) ^ (words[i - 15] >>> 3)
    words[i] = (s1 + words[i - 7] + s0 + words[i - 16]) >>> 0
  }
}

function gecInit(d7) {
  const words = new Array(16).fill(0)
  words[14] = ((d7[0] << 16) | (d7[1] << 8) | d7[2]) >>> 0
  words[15] = ((d7[3] << 24) | (d7[4] << 16) | (d7[5] << 8) | d7[6]) >>> 0
  for (let i = 16; i <= 29; i += 1) {
    let v = GEC_SCHEDULE[i - 16]
    if (i - 2 >= 14) v += i === 16 ? (gecSigma1(words[i - 2]) ^ GEC_SIGMA1_ADJUSTMENT) : gecSigma1(words[i - 2])
    if (i - 7 >= 14) v += words[i - 7]
    if (i - 15 >= 14) v += i === 29 ? (gecSigma0(words[i - 15]) ^ GEC_SIGMA0_ADJUSTMENT) : gecSigma0(words[i - 15])
    words[i] = v >>> 0
  }
  words[30] = (gecSigma1(words[28]) + words[23] + gecSigma0(words[15]) + words[14]
    + GEC_ADJUSTMENT - GEC_K[14]) >>> 0
  gecExpand(words, 31)
  const working = gecRounds(GEC_SHA256_STATE, words, 14)
  return GEC_SHA256_INITIAL.map((x, i) => (x + working[i]) >>> 0)
}

function gecDigest(data) {
  const length = 57 + data.length
  const padLen = (((55 - length) % 64) + 64) % 64
  const tail = Buffer.alloc(8)
  tail.writeBigUInt64BE(BigInt(length * 8))
  const padded = Buffer.concat([data.subarray(7), Buffer.from([0x80]), Buffer.alloc(padLen), tail])
  let state = gecInit(data.subarray(0, 7))
  for (let off = 0; off < padded.length; off += 64) {
    const words = []
    for (let i = 0; i < 16; i += 1) words.push(padded.readUInt32BE(off + i * 4))
    gecExpand(words)
    const working = gecRounds(state, words)
    state = state.map((x, i) => (x + working[i]) >>> 0)
  }
  const out = Buffer.alloc(32)
  state.forEach((v, i) => out.writeUInt32BE(v >>> 0, i * 4))
  return out
}

function gecWords(buf) {
  const padded = Buffer.concat([buf, Buffer.alloc((4 - (buf.length % 4)) % 4)])
  const out = []
  for (let i = 0; i < padded.length; i += 4) out.push(padded.readUInt32LE(i))
  if (out.length % 2) out.push(0)
  return out
}

function gecCs64(data, digest) {
  const keyA = digest.readUInt32LE(0) | 1
  const keyB = digest.readUInt32LE(4) | 1
  const factors = [imul32(keyA, GEC_FACTOR), imul32(keyB, GEC_FACTOR)]
  const terms = []
  const offsets = []
  GEC_COEFFICIENTS.forEach(([coefficient, offset], index) => {
    terms.push(imul32(coefficient, factors[index % 2]))
    offsets.push(offset)
  })
  gecWords(Buffer.concat([Buffer.from([0x00]), data])).forEach((word, index) => {
    let term = index % 2 === 0 ? imul32(word, keyA) : imul32(word, keyB)
    let offset = index % 2 === 0 ? imul32(rol16(word), 0xEF0569FB) : imul32(rol16(word), 0xC31713DB)
    if (index === 0) {
      term = (term + imul32(GEC_ADJUSTMENT0, factors[0])) >>> 0
      offset = (offset + GEC_ADJUSTMENT1) >>> 0
    }
    terms.push(term >>> 0)
    offsets.push(offset >>> 0)
  })
  let swapAcc = 0
  let swapLast = 0
  let state = 0
  let acc = 0
  for (let i = 0; i < terms.length; i += 2) {
    const x = terms[i]
    const y = terms[i + 1]
    let t = rol16((offsets[i] + x) >>> 0)
    let q = imul32(t, 0x76F50F34)
    swapAcc = (swapAcc + ((imul32(rol16(q), 0xC3EFEA97) + q) >>> 0)) >>> 0
    t = rol16((offsets[i + 1] + y) >>> 0)
    q = imul32(t, 0x1881CE3C)
    swapLast = (imul32(rol16(q), 0x35BD1EC9) + q) >>> 0
    swapAcc = (swapAcc + swapLast) >>> 0

    state = (imul32(state, keyA) + x) >>> 0
    state = imul32(rol16(state), 0xCF98B111)
    state = imul32(rol16(state), 0x87085B9F)
    state = imul32(rol16(state), 0x12CEB96D)
    state = imul32(rol16(state), 0x257E1D83)
    acc = (acc + state) >>> 0
    t = (imul32(state, keyB) + y) >>> 0
    t = imul32(rol16(t), 0xA27416F5)
    t = imul32(rol16(t), 0xD38396FF)
    state = imul32(rol16(imul32(rol16(t), 0x7C932B89)), 0xBFA49F69)
    acc = (acc + state) >>> 0
  }
  return [(swapLast ^ state) >>> 0, (swapAcc ^ acc) >>> 0]
}

// 令牌本身：秒数 → 5 分钟取整 → Windows file time（100ns）→ 拼上 token 做摘要。
// 时间戳必须精确到 Python 那种「`.0f` 打出来的整数字符串」，所以走 BigInt（超过 2^53 了）
function edgeGec(unixSeconds) {
  let ticks = unixSeconds === undefined ? Date.now() / 1000 : Number(unixSeconds)
  ticks += EDGE.winEpoch
  ticks -= ticks % 300                       // 取整到 5 分钟
  ticks *= 1e7                               // Windows file time（100ns）
  const data = Buffer.from(`${BigInt(Math.round(ticks))}${EDGE.token}`, 'ascii')
  const digest = gecDigest(data)
  const [low, high] = gecCs64(data, digest)
  const cs = Buffer.alloc(8)
  cs.writeUInt32LE(low, 0)
  cs.writeUInt32LE(high, 4)
  return `${cs.toString('hex').toUpperCase()}-${digest.toString('hex').toUpperCase()}`
}

// 连 wss 用的 WebSocket 实现：优先 ws 这个包（老 node 也行），没有就用 Node 22+ 自带的
// （自带那支也认 `{ headers }`，微软要的 Origin / muid 才带得出去）。
// 两条都没有才报错——以前只认 ws，dsh 那份 node_modules 一重装（ws 是它的传递依赖）
// 整条 Edge 就哑了。
function tryWs() {
  const require = createRequire(import.meta.url)
  const roots = [
    (process.env.QUANTA_VOICE_ENGINE || '').trim(),
    path.join(HERE, 'node_modules'),
    path.join(HERE, '..', '..', 'node_modules'),          // 打包后：Resources/node_modules
    path.join(dataDir(), 'dsh', 'node_modules'),
  ].filter(Boolean)
  for (const root of roots) {
    try {
      return require(require.resolve('ws', { paths: [root] }))
    } catch (error) { /* 换下一个 */ }
  }
  return null
}

// 把「ws」和「内置 WebSocket」抹平成同一套回调：open(send) / text / binary / error。
// 返回一个关闭函数。
function connectEdge(url, headers, handlers) {
  const ws = tryWs()
  if (ws) {
    const socket = new ws(url, { headers })
    socket.on('open', () => handlers.open((text) => socket.send(text)))
    socket.on('message', (data, isBinary) => {
      if (isBinary) handlers.binary(Buffer.from(data))
      else handlers.text(String(data))
    })
    socket.on('error', (error) => handlers.error(new Error(`Edge 连接失败：${error.message}`)))
    return () => { try { socket.terminate() } catch (error) { /* 已经关了 */ } }
  }
  if (typeof WebSocket !== 'function') {
    handlers.error(new Error('连不了 Edge 语音：没有 ws 包，这个 node 也没有内置 WebSocket'
      + '（装 node 22+，或在 dsh 的 node_modules 里补一个 ws）'))
    return () => {}
  }
  const socket = new WebSocket(url, { headers })
  socket.binaryType = 'arraybuffer'
  socket.onopen = () => handlers.open((text) => socket.send(text))
  socket.onmessage = (event) => {
    if (typeof event.data === 'string') handlers.text(event.data)
    else handlers.binary(Buffer.from(event.data))
  }
  socket.onerror = () => handlers.error(new Error('Edge 连接失败（内置 WebSocket）'))
  return () => { try { socket.close() } catch (error) { /* 同上 */ } }
}

function edgeSay(text, voice, speed) {
  const major = EDGE.version.split('.')[0]
  const connection = randomBytes(16).toString('hex')
  const muid = randomBytes(16).toString('hex').toUpperCase()
  const url = 'wss://speech.platform.bing.com/consumer/speech/synthesize/readaloud/edge/v1'
    + `?TrustedClientToken=${EDGE.token}&Sec-MS-GEC=${edgeGec()}`
    + `&Sec-MS-GEC-Version=${EDGE.gecVersion}&ConnectionId=${connection}`
  const rate = Math.round(((Number(speed) || 1) - 1) * 100)
  const ssml = `<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='zh-CN'>`
    + `<voice name='${voice || 'zh-CN-XiaoxiaoNeural'}'>`
    + `<prosody pitch='+0Hz' rate='${rate >= 0 ? '+' : ''}${rate}%' volume='+0%'>`
    + String(text).replace(/[<>&]/g, (ch) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[ch]))
    + `</prosody></voice></speak>`
  return new Promise((resolve, reject) => {
    const headers = {
      'Pragma': 'no-cache', 'Cache-Control': 'no-cache',
      'Origin': 'chrome-extension://jdiccldimpdaibmpdkjnbmckianbfold',
      'Accept-Encoding': 'gzip, deflate, br, zstd', 'Accept-Language': 'en-US,en;q=0.9',
      'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        + `(KHTML, like Gecko) Chrome/${major}.0.0.0 Safari/537.36 Edg/${major}.0.0.0`,
      'Cookie': `muid=${muid};`,
    }
    const chunks = []
    let timer = null
    let settled = false
    let closeFn = () => {}
    const done = (error, value) => {
      if (settled) return
      settled = true
      if (timer) clearTimeout(timer)
      closeFn()
      if (error) reject(error)
      else resolve(value)
    }
    timer = setTimeout(() => done(new Error('Edge 语音超时（网络？）')), 25000)
    closeFn = connectEdge(url, headers, {
      open: (send) => {
        const now = new Date().toString()
        send(`X-Timestamp:${now}\r\nContent-Type:application/json; charset=utf-8\r\n`
          + `Path:speech.config\r\n\r\n{"context":{"synthesis":{"audio":`
          + `{"metadataoptions":{"sentenceBoundaryEnabled":"false","wordBoundaryEnabled":"false"},`
          + `"outputFormat":"audio-24khz-48kbitrate-mono-mp3"}}}}`)
        send(`X-RequestId:${connection}\r\nContent-Type:application/ssml+xml\r\n`
          + `X-Timestamp:${now}Z\r\nPath:ssml\r\n\r\n${ssml}`)
      },
      text: (data) => {
        if (!data.includes('Path:turn.end')) return
        const audio = Buffer.concat(chunks)
        if (!audio.length) done(new Error('Edge 没返回音频'))
        else done(null, { audio, format: 'mp3', sampleRate: 24000,
                          seconds: (audio.length * 8) / 48000 })   // 48kbps 估时长
      },
      binary: (buf) => {
        if (buf.length < 4) return
        const headerLen = buf.readUInt16BE(0)
        if (buf.subarray(2, 2 + headerLen).toString().includes('Path:audio')) {
          chunks.push(buf.subarray(2 + headerLen))
        }
      },
      error: (error) => done(error),
    })
  })
}

let cached = null
function buildTts() {
  if (cached) return cached
  const sherpa = requireEngine()
  const dir = modelDir(ENGINE)
  cached = new sherpa.OfflineTts({
    model: ENGINES[ENGINE].build(dir),
    numThreads: 2,
    provider: 'cpu',
  })
  return cached
}

// Float32 采样 → 16 位单声道 WAV
function wavBytes(samples, sampleRate) {
  const dataBytes = samples.length * 2
  const out = Buffer.alloc(44 + dataBytes)
  out.write('RIFF', 0)
  out.writeUInt32LE(36 + dataBytes, 4)
  out.write('WAVE', 8)
  out.write('fmt ', 12)
  out.writeUInt32LE(16, 16)
  out.writeUInt16LE(1, 20)
  out.writeUInt16LE(1, 22)
  out.writeUInt32LE(sampleRate, 24)
  out.writeUInt32LE(sampleRate * 2, 28)
  out.writeUInt16LE(2, 32)
  out.writeUInt16LE(16, 34)
  out.write('data', 36)
  out.writeUInt32LE(dataBytes, 40)
  for (let i = 0; i < samples.length; i += 1) {
    const clamped = Math.max(-1, Math.min(1, samples[i]))
    out.writeInt16LE(Math.round(clamped * 32767), 44 + i * 2)
  }
  return out
}

// 系统语音兜底：先挑一个**真的装着**的中文嗓子。以前写死 `-v Tingting`，
// 可 macOS 不一定装着这支（用户机器上就没有过），`say` 会直接报「voice not found」，
// 整句就静悄悄地没声了——比没有兜底还糟。
let macVoiceCache = null
async function macVoice() {
  if (macVoiceCache !== null) return macVoiceCache
  macVoiceCache = ''
  try {
    const { execFile } = await import('node:child_process')
    const listing = await new Promise((resolve, reject) => {
      execFile('say', ['-v', '?'], { timeout: 8000 }, (error, stdout) => (
        error ? reject(error) : resolve(stdout)))
    })
    // 一行长这样：Tingting            zh_CN    # 婷婷
    const zh = []
    for (const line of String(listing).split('\n')) {
      const hit = line.match(/^(.+?)\s{2,}(zh_[A-Za-z]+)/)
      if (hit) zh.push(hit[1].trim())
    }
    const preferred = ['Tingting', 'Ting-Ting', 'Mei-Jia', 'Sinji', 'Li-mu', 'Yu-shu']
    macVoiceCache = preferred.find((name) => zh.includes(name)) || zh[0] || ''
  } catch (error) { /* 列不出来：就用系统默认嗓子 */ }
  return macVoiceCache
}

// Edge 连不上时的兜底：用 macOS 自带的 say 念（本地、零下载、一定能出声）。
// 音色是系统那个中文语音，效果一般，但总比整句回复没声强
async function macSay(text, speed) {
  const { execFile } = await import('node:child_process')
  const { mkdtemp } = await import('node:fs/promises')
  const work = await mkdtemp(path.join(os.tmpdir(), 'quanta-say-'))
  const aiff = path.join(work, 'say.aiff')
  const wav = path.join(work, 'say.wav')
  const run = (bin, args) => new Promise((resolve, reject) => {
    execFile(bin, args, { timeout: 30000 }, (error) => (error ? reject(error) : resolve()))
  })
  const rate = Math.round(Math.max(120, Math.min(400, 190 * (Number(speed) || 1))))
  const voice = await macVoice()
  const speakArgs = [...(voice ? ['-v', voice] : []), '-r', String(rate), '-o', aiff, text]
  await run('say', speakArgs)
  await run('afconvert', ['-f', 'WAVE', '-d', 'LEI16@16000', '-c', '1', aiff, wav])
  const bytes = await (await import('node:fs/promises')).readFile(wav)
  await (await import('node:fs/promises')).rm(work, { recursive: true, force: true })
  const seconds = Math.max(0.1, (bytes.length - 44) / 2 / 16000)
  return { audio: bytes, format: 'wav', sampleRate: 16000, seconds, voice: voice || '默认' }
}

async function say(text, voice, speed) {
  if (ENGINES[ENGINE].network) {
    const hit = ENGINES[ENGINE].voices.find((row) => row[0] === voice)
    const name = hit ? hit[0] : ENGINES[ENGINE].voices[0][0]
    let lastError = null
    for (let attempt = 0; attempt < 2; attempt += 1) {     // 网络偶发超时：重试一次
      try {
        const out = await edgeSay(text, name, speed)
        return { audio: out.audio, format: out.format, sampleRate: out.sampleRate,
                 seconds: out.seconds }
      } catch (error) {
        lastError = error
        report({ event: 'note', text: `Edge 第 ${attempt + 1} 次没成：${error.message}` })
        await new Promise((done) => setTimeout(done, 600))
      }
    }
    // 两次都没成：退回系统语音，别让用户听不到回复
    report({ event: 'note', text: `Edge 连不上（${lastError && lastError.message}），改用系统语音念` })
    const out = await macSay(text, speed)
    return { ...out, fallback: true }
  }
  const tts = buildTts()
  const audio = tts.generate({ text, sid: voiceSid(ENGINE, voice), speed: Number(speed) || 1 })
  return { samples: audio.samples, sampleRate: audio.sampleRate, format: 'wav' }
}

// ── 常驻服务：桥把句子递过来，这边把 wav 递回去 ──────────────────
function serve() {
  report({ event: 'note', text: `TTS 引擎：${ENGINES[ENGINE].label}` })
  const rl = createInterface({ input: process.stdin })
  // 一条一条按顺序合成（Edge 是网络往返，异步；本地引擎也是几秒级的活）。
  // 排队保证「第 1 句的音频」一定先于「第 2 句」出去。
  let chain = Promise.resolve()
  rl.on('line', (line) => {
    const trimmed = line.trim()
    if (!trimmed) return
    let payload = null
    try { payload = JSON.parse(trimmed) } catch (error) { return }
    const id = payload.id == null ? 0 : payload.id
    if (payload.stop) process.exit(0)         // 桥那边收工时会发这个
    if (payload.warmup) {                     // 预热：本地模型加载好；Edge 没什么可热的
      chain = chain.then(() => {
        try {
          if (!ENGINES[ENGINE].network) buildTts()
          report({ event: 'ready' })
        } catch (error) {
          report({ event: 'error', id, error: error.message })
        }
      })
      return
    }
    const text = String(payload.text || '').trim()
    if (!text) { report({ event: 'audio', id, format: 'wav', wav: '', seconds: 0 }); return }
    chain = chain.then(async () => {
      const started = Date.now()
      try {
        const out = await say(text, payload.voice, payload.speed)
        const audio = out.format === 'wav' ? wavBytes(out.samples, out.sampleRate) : out.audio
        report({ event: 'audio', id, format: out.format, sampleRate: out.sampleRate,
                 fallback: !!out.fallback,
                 seconds: out.format === 'wav' ? out.samples.length / out.sampleRate : out.seconds,
                 took: (Date.now() - started) / 1000, wav: audio.toString('base64') })
      } catch (error) {
        report({ event: 'error', id, error: error && error.message ? error.message : String(error) })
      }
    })
  })
}

// ── 命令行 ──────────────────────────────────────────────────
function flag(name, fallback = '') {
  const args = process.argv.slice(2)
  const at = args.indexOf('--' + name)
  return at >= 0 && args[at + 1] ? args[at + 1] : fallback
}

async function main() {
  const args = process.argv.slice(2)
  if (args.includes('--status')) { await status(); return }
  if (args.includes('--download')) {
    if (!(await takeLock())) {
      report({ event: 'note', text: '已经有一份在下（同一时间只跑一份）' })
      report({ event: 'already' })
      return
    }
    try {
      await ensureEngine(ENGINE, args.includes('--reset'))
      await status()
    } finally {
      await dropLock()
    }
    return
  }
  if (args.includes('--serve')) { serve(); return }
  if (args.includes('--say')) {
    const text = flag('say')
    const out = flag('out')
    const info = await readyInfo()
    if (!info.ready) {
      report({ event: 'error', error: `朗读模型（${ENGINES[ENGINE].label}）还没下完（先跑 --download）` })
      process.exit(1)
    }
    const result = await say(text, flag('voice', ''), flag('speed', '1'))
    const audio = result.format === 'wav' ? wavBytes(result.samples, result.sampleRate) : result.audio
    if (out) await writeFile(out, audio)
    else process.stdout.write(audio)        // 没给 --out 就往 stdout 吐音频字节
    report({ event: 'text', format: result.format, engine: ENGINE, out,
             seconds: result.format === 'wav' ? result.samples.length / result.sampleRate
                                              : result.seconds,
             sampleRate: result.sampleRate })
    return
  }
  console.error('用法：node tts.mjs --status | --download [--reset] | --say "文本" [--out 文件] | --serve'
    + '（引擎用 QUANTA_TTS_ENGINE=edge|kokoro|piper 选）')
  process.exit(2)
}

main().catch((error) => {
  report({ event: 'error', error: error && error.message ? error.message : String(error) })
  process.exit(1)
})
