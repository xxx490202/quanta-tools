#!/usr/bin/env node
// 语音输入那条侧车：本地 SenseVoice（sherpa-onnx）把一段 WAV 转成文字。
//
// 为什么自己起一条而不是用 dsh 那个插件：dsh 的实验性语音包（dsh-experimental-voice-input-bundle）
// 是给 **dsh 自己的网页前端**用的——录音 UI 是 web 插件、转写服务只挂在 dsh 的 typert 远程
// （WebSocket /api/remote.mux）上，而 Quanta 跟 dsh 之间只有 ACP（只有 session/*），够不着。
// 所以这里只借它的两样东西：**引擎**（dsh 装的那份 sherpa-onnx-node + darwin-arm64 原生库，33MB，
// 已经在盘上）和**它锁定的模型**（下面的 url/sha256 是从它的 runtime/assets.json 抄来的，
// 一个字节都不差地校验）。
//
// 三个模式：
//   --download          只下模型（int8 + tokens + VAD，带进度和 sha256 校验）
//   --wav <文件>         转写一个 WAV，把文字打到 stdout（现在的最小验证走这条）
//   （接界面时再加常驻模式：从套接字收 WAV、回文字，见 bridge 那边的用量）
//
// 模型落 <数据目录>/voice/sensevoice/（下过一次就一直用）。

import { createRequire } from 'node:module'
import { createHash } from 'node:crypto'
import { createWriteStream } from 'node:fs'
import { mkdir, stat, rename, unlink, readFile, writeFile } from 'node:fs/promises'
import { Readable } from 'node:stream'
import { pipeline } from 'node:stream/promises'
import path from 'node:path'
import os from 'node:os'
import process from 'node:process'
import { ASR_ENGINES, asrEngine, pythonForMlx, vadPath, VAD_ASSET } from './asr-models.mjs'

const HERE = path.dirname(new URL(import.meta.url).pathname)

// 这次用哪个识别引擎（界面那个选择器 -> 桥 -> 环境变量 QUANTA_ASR_ENGINE）
const ENGINE = asrEngine()

// python 引擎（MLX）：把整条命令行原样转给它——协议一样（[voice] {json}），
// 所以桥那边一个字都不用改。
if (ASR_ENGINES[ENGINE].runtime === 'python'
    && !process.argv.slice(2).includes('--status')) {
  // --status 留在 node 这边（要带上「每个引擎各一份结论」给设置页那个选择器）；
  // --wav / --download 转给 python 那份（协议一样：[voice] {json}）
  const { spawnSync } = await import('node:child_process')
  const python = pythonForMlx()
  if (!python) {
    console.error('[voice] 找不到装了 mlx-audio 的 python（pip install mlx-audio）')
    process.exit(1)
  }
  const script = path.join(HERE, ASR_ENGINES[ENGINE].script)
  const repo = ASR_ENGINES[ENGINE].repo || ''
  const args = [script, ...process.argv.slice(2), '--model', modelDir(ENGINE)]
  if (repo) args.push('--repo', repo)
  const out = spawnSync(python, args, { stdio: 'inherit',
    // HF 新的 Xet 存储走镜像会 401：关掉它，走普通 HTTPS（镜像能代理）
    env: { ...process.env, HF_HUB_DISABLE_XET: '1' } })
  process.exit(out.status == null ? 1 : out.status)
}

// 官方（dsh 那份 assets.json）锁定的文件：url / 字节数 / sha256
// 国内拉 huggingface 常常连不上：同一个路径换到 hf-mirror（官方镜像）再试一次
const MIRRORS = [null, 'hf-mirror.com']

function dataDir() {
  const env = (process.env.QUANTA_DATA_DIR || '').trim()
  if (env) return env
  return path.join(os.homedir(), 'Library', 'Application Support', 'Quantification')
}

function modelDir(engine = ENGINE) {
  const env = (process.env.QUANTA_VOICE_MODELS || '').trim()
  if (env && engine === ENGINE) return env
  return path.join(dataDir(), 'voice', ASR_ENGINES[engine].dir)
}

// 当前引擎要下的文件：sensevoice 那份是官方锁定的三个（带 sha256），
// 其它引擎按大小核对（清单在 asr-models.mjs）
function assetsFor(engine) {
  const table = ASR_ENGINES[engine]
  if (table.assets) return table.assets.map((row) => ({ ...row, url: null }))
  if (table.files) return table.files.map((name) => ({ name, bytes: 0 }))
  return []
}

// 断句用的 silero VAD（公共一份，和识别引擎无关）：下到 <数据目录>/voice/vad/
async function ensureVad() {
  const file = vadPath()
  if (await fileOk(file, VAD_ASSET)) return
  console.error(`[voice] 下载 ${VAD_ASSET.name}（${(VAD_ASSET.bytes / 1048576).toFixed(1)}MB）…`)
  await download(VAD_ASSET, file)
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

// ── 下载 ────────────────────────────────────────────────────
async function fileOk(file, asset) {
  try {
    const info = await stat(file)
    if (!info.isFile()) return false
    if (asset.bytes && info.size !== asset.bytes) return false   // 没记大小的只看在不在
  } catch (error) {
    return false
  }
  if (!asset.sha256) return true                    // 只锁了大小的那几个（paraformer）
  const digest = createHash('sha256')
  await pipeline((await import('node:fs')).createReadStream(file), digest)  // 流进 hash 里，不占内存
  return digest.digest('hex') === asset.sha256
}

// 进度回报：一行 JSON（桥那边照着画进度条），人看着也好读
function report(payload) {
  console.error('[voice] ' + JSON.stringify(payload))
}

const ASR_REPO = {
  'qwen3-mlx': 'mlx-community/Qwen3-ASR-1.7B-8bit',
  'qwen3-mlx-0.6b': 'mlx-community/Qwen3-ASR-0.6B-8bit',
}

async function download(asset, file, engine = ENGINE) {
  const part = file + '.part'
  const base = asset.url || `https://huggingface.co/${ASR_REPO[engine]}/resolve/main/${asset.name}`
  let lastError = null
  for (const mirror of MIRRORS) {
    const url = mirror ? base.replace('huggingface.co', mirror) : base
    try {
      const response = await fetch(url, { redirect: 'follow' })
      if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`)
      await mkdir(path.dirname(file), { recursive: true })
      let got = 0
      let last = 0
      const digest = createHash('sha256')
      const counter = new TransformStream({
        transform(chunk, controller) {
          got += chunk.length
          digest.update(chunk)
          const now = Date.now()
          if (now - last > 1000) {            // 一秒一行，别把日志刷爆
            last = now
            report({ event: 'download', file: asset.name, done: got, total: asset.bytes,
                     source: mirror || 'huggingface.co' })
          }
          controller.enqueue(chunk)
        },
      })
      await pipeline(Readable.fromWeb(response.body.pipeThrough(counter)), createWriteStream(part))
      const hex = digest.digest('hex')
      if (got !== asset.bytes || hex !== asset.sha256) {
        throw new Error(`校验没过（拿到 ${got} 字节）`)
      }
      await rename(part, file)
      return
    } catch (error) {
      lastError = error
      await unlink(part).catch(() => {})
      console.error(`[voice] ${mirror || 'huggingface.co'} 没成功：${error.message}`)
    }
  }
  throw new Error(`下不动 ${asset.name}：${lastError && lastError.message}`)
}

// 下载锁：同一时间只允许一份在跑（界面上点一下、外面又下一份，两份会互相写花 .part）。
// 锁里写着持有者的 pid，pid 不在了就当锁失效（和 tts.mjs 那边一个做法）
function lockFile() {
  return path.join(modelDir(), 'download.lock')
}

async function takeLock() {
  try {
    const raw = await readFile(lockFile(), 'utf8')
    const holder = Number((raw || '').trim()) || 0
    if (holder && holder !== process.pid) {
      try {
        process.kill(holder, 0)
        return false
      } catch (error) { /* 不在了，锁可以抢 */ }
    }
  } catch (error) { /* 没有锁文件 */ }
  try {
    await mkdir(modelDir(), { recursive: true })
    await writeFile(lockFile(), String(process.pid))
  } catch (error) { /* 写不进去也照下 */ }
  return true
}

async function dropLock() {
  await unlink(lockFile()).catch(() => {})
}

async function ensureModels() {
  const dir = modelDir()
  console.error(`[voice] 识别引擎：${ASR_ENGINES[ENGINE].label}，模型目录：${dir}`)
  await ensureVad()
  for (const asset of assetsFor(ENGINE)) {
    const file = path.join(dir, asset.name)
    if (await fileOk(file, asset)) {
      console.error(`[voice] ${asset.name} 已在（${(asset.bytes / 1048576).toFixed(1)}MB）`)
      continue
    }
    console.error(`[voice] 下载 ${asset.name}（${(asset.bytes / 1048576).toFixed(1)}MB）…`)
    const started = Date.now()
    await download(asset, file, ENGINE)
    console.error(`[voice] ${asset.name} 好了（${((Date.now() - started) / 1000).toFixed(1)}s）`)
  }
  return dir
}

// ── 转写 ────────────────────────────────────────────────────
function buildRecognizer(sherpa, dir) {
  return new sherpa.OfflineRecognizer({
    featConfig: { sampleRate: 16000, featureDim: 80 },
    modelConfig: { ...ASR_ENGINES[ENGINE].config(dir),
      ...(ASR_ENGINES[ENGINE].tokens(dir) ? { tokens: ASR_ENGINES[ENGINE].tokens(dir) } : {}),
      numThreads: Math.max(1, Math.min(4, os.cpus().length - 1)),
      provider: 'cpu',
      debug: 0,
    },
  })
}

function transcribe(sherpa, recognizer, file) {
  const wave = sherpa.readWave(file)
  const stream = recognizer.createStream()
  stream.acceptWaveform({ sampleRate: wave.sampleRate, samples: wave.samples })
  recognizer.decode(stream)
  return String(recognizer.getResult(stream).text || '').trim()
}

// ── 命令行 ─────────────────────────────────────────────────
async function main() {
  const args = process.argv.slice(2)
  const flag = (name) => {
    const at = args.indexOf(name)
    return at >= 0 ? (args[at + 1] || '') : ''
  }
  const wantDownload = args.includes('--download')
  const wantStatus = args.includes('--status')
  const wav = flag('--wav')

  if (!wantDownload && !wantStatus && !wav) {
    console.error('用法：node asr.mjs --status | --download [--reset] | --wav <文件>')
    process.exit(2)
  }
  if (args.includes('--reset')) {
    // 「重新下载」：先把三份文件删掉（校验过的那份也会删），下面 --download 从头来
    const dir = modelDir()
    for (const asset of assetsFor(ENGINE)) {
      await unlink(path.join(dir, asset.name)).catch(() => {})
      await unlink(path.join(dir, asset.name + '.part')).catch(() => {})
    }
    console.error('[voice] 旧的模型文件已清掉')
  }
  if (wantStatus) {
    // 当前引擎的文件齐不齐 + 每个引擎各一份结论（界面那个选择器要用）
    const dir = modelDir()
    let engine = ''
    try { requireEngine() } catch (error) { engine = error.message }
    const files = {}
    for (const asset of assetsFor(ENGINE)) {
      files[asset.name] = await fileOk(path.join(dir, asset.name), asset)
    }
    files['silero_vad.onnx（断句用）'] = await fileOk(vadPath(), VAD_ASSET)
    if (ASR_ENGINES[ENGINE].runtime === 'python') {
      const python = pythonForMlx()
      files['python + mlx-audio'] = !!python
      if (!python) engine = '找不到装了 mlx-audio 的 python（pip install mlx-audio）'
    }
    const asrEngines = {}
    for (const key of Object.keys(ASR_ENGINES)) {
      if (key === ENGINE) { asrEngines[key] = Object.values(files).every(Boolean); continue }
      let ready = true
      for (const asset of assetsFor(key)) {
        if (!(await fileOk(path.join(modelDir(key), asset.name), asset))) { ready = false; break }
      }
      if (ready && ASR_ENGINES[key].runtime === 'python' && !pythonForMlx()) ready = false
      asrEngines[key] = ready
    }
    console.log(JSON.stringify({ engine: ENGINE, label: ASR_ENGINES[ENGINE].label, models: dir,
      error: engine, files, asrEngines,
      engines: Object.keys(ASR_ENGINES).map((key) => ({ key, label: ASR_ENGINES[key].label,
        note: ASR_ENGINES[key].note || '', ready: !!asrEngines[key] })),
      ready: !engine && Object.values(files).every(Boolean) }))
    return
  }
  if (wantDownload && !(await takeLock())) {
    console.error('[voice] 已经有一份在下了（同一时间只跑一份）')
    process.exit(0)
  }
  try {
    const dir = await ensureModels()
    if (wantDownload) {
      console.log(dir)
      return
    }
  } finally {
    if (wantDownload) await dropLock()
  }
  const sherpa = requireEngine()
  const started = Date.now()
  const recognizer = buildRecognizer(sherpa, dir)
  const text = transcribe(sherpa, recognizer, wav)
  const took = (Date.now() - started) / 1000
  console.error(`[voice] 转写用了 ${took.toFixed(2)}s`)
  console.log(text)                                  // 人看的：纯文字一行
  console.error('[voice] ' + JSON.stringify({ event: 'text', text, seconds: took }))
}

main().catch((error) => {
  console.error(`[voice] 出错：${error && error.message}`)
  process.exit(1)
})
