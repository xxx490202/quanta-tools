#!/usr/bin/env node
// Rhema 的耳朵：常驻监听侧车。VAD（silero）负责断句，SenseVoice 负责把那句话转成文字。
//
//   node rhema.mjs --serve
//
// stdin（一行一条 JSON）：
//   {"pcm": "<base64 的 16k 单声道 s16le>"}   渲染层每 ~100ms 喂一片
//   {"reset": true}                           丢掉手上的 VAD 状态（重新开始听）
//   {"stop": true}                            收工退出
// stderr（一行一条 JSON，前缀 [voice] 和转写侧车同一套）：
//   {"event":"ready"}                         模型都起来了，可以开始喂音频
//   {"event":"speech_start"}                  检测到人开口（界面拿它打断朗读）
//   {"event":"text","text":"…","seconds":1.4} 一句话听完、转好了
//   {"event":"error","error":"…"}
//
// 断句参数照 HuggingFace speech-to-speech 那套（阈值 0.5），静音判定稍微放长到 0.45s：
// 语音对话里半秒的停顿比 0.3s 更接近人的「说完了」。
import { createRequire } from 'node:module'
import { createInterface } from 'node:readline'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawn } from 'node:child_process'
import { ASR_ENGINES, asrEngine, pythonForMlx, vadPath } from './asr-models.mjs'

const HERE = path.dirname(fileURLToPath(import.meta.url))

const VAD = {
  threshold: 0.5,
  minSilenceDuration: 0.45,      // 静音这么久就算说完了
  minSpeechDuration: 0.25,       // 太短的「喂」不当一句话
  maxSpeechDuration: 15,         // 一口气说太长就切开，别攒着
  windowSize: 512,
}

function dataDir() {
  const env = (process.env.QUANTA_DATA_DIR || '').trim()
  if (env) return env
  return path.join(os.homedir(), 'Library', 'Application Support', 'Quantification')
}

function voiceDir(engine) {
  return path.join(dataDir(), 'voice', ASR_ENGINES[engine].dir)
}

function modelDir() {
  return (process.env.QUANTA_VOICE_MODELS || '').trim() || voiceDir(asrEngine())
}

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

// python 引擎（MLX）：VAD 还是这边断（silero），每段音频转给 mlx_asr.py --serve。
// 它的 stderr 也是 `[voice] {json}`，直接透传——桥那边不用区分是谁在报。
let external = null
let externalSeq = 0

function startExternal(engine) {
  const python = pythonForMlx()
  if (!python) {
    report({ event: 'error', error: '找不到装了 mlx-audio 的 python（pip install mlx-audio）' })
    return null
  }
  const script = path.join(HERE, ASR_ENGINES[engine].script)
  const proc = spawn(python, [script, '--serve', '--model', modelDir()],
    { cwd: HERE, env: process.env,
    stdio: ['pipe', 'pipe', 'pipe'] })
  proc.stdout.on('data', () => { /* 它的 stdout 是给人看的文字，忽略 */ })
  createInterface({ input: proc.stderr }).on('line', (line) => {
    const at = line.indexOf('{')
    if (at < 0) {
      if (line.trim()) log(`[mlx] ${line.trim()}`)
      return
    }
    try { report(JSON.parse(line.slice(at))) } catch (error) { /* 不是 JSON 就算了 */ }
  })
  proc.on('exit', () => {
    if (external === proc) external = null
    report({ event: 'error', error: 'MLX 转写进程退出了' })
    report({ event: 'closed' })
  })
  return proc
}

// Float32 采样 → 16 位小端 PCM → base64（mlx_asr.py 收的就是这个）
function pcmBase64(samples) {
  const bytes = Buffer.alloc(samples.length * 2)
  for (let i = 0; i < samples.length; i += 1) {
    const value = Math.max(-1, Math.min(1, samples[i]))
    bytes.writeInt16LE(Math.round(value * 32767), i * 2)
  }
  return bytes.toString('base64')
}

// 侧车的日志行（[rhema:listen] xxx）：没装 log 就打到 stderr
function log(text) {
  process.stderr.write(text + '\n')
}

function report(payload) {
  console.error('[voice] ' + JSON.stringify(payload))
}

function serve() {
  const sherpa = requireEngine()
  const engine = asrEngine()
  const dir = modelDir()
  const vad = new sherpa.Vad({
    sileroVad: {
      // 断句用的 silero VAD 只在 sensevoice 那份里有一份，所有引擎共用
      model: vadPath(),
      threshold: VAD.threshold,
      minSilenceDuration: VAD.minSilenceDuration,
      minSpeechDuration: VAD.minSpeechDuration,
      maxSpeechDuration: VAD.maxSpeechDuration,
      windowSize: VAD.windowSize,
    },
    sampleRate: 16000,
    numThreads: 1,
    provider: 'cpu',
  }, 30)
  const pythonEngine = ASR_ENGINES[engine].runtime === 'python'
  let recognizer = null
  if (pythonEngine) {
    external = startExternal(engine)          // 它自己会报一条 ready（透传上去）
  } else {
    recognizer = new sherpa.OfflineRecognizer({
      featConfig: { sampleRate: 16000, featureDim: 80 },
      modelConfig: { ...ASR_ENGINES[engine].config(dir),
        ...(ASR_ENGINES[engine].tokens(dir) ? { tokens: ASR_ENGINES[engine].tokens(dir) } : {}),
        numThreads: 2, provider: 'cpu' },
    })
    report({ event: 'ready', asr: engine, label: ASR_ENGINES[engine].label })
  }

  let speaking = false
  const flush = () => {                    // 把攒下的整段拿去做转写
    const segment = vad.front(false)
    vad.pop()
    const samples = segment && segment.samples
    speaking = false
    if (!samples || samples.length < 1600) return          // 太短（<0.1s）：当噪声丢掉
    const seconds = samples.length / 16000
    if (external) {
      // python 引擎：整段丢给它转（它转完自己报 text）
      try {
        externalSeq += 1
        external.stdin.write(JSON.stringify({ pcm: pcmBase64(samples), id: externalSeq }) + '\n')
      } catch (error) {
        report({ event: 'error', error: `喂给 MLX 失败：${error.message}` })
      }
      return
    }
    const started = Date.now()
    const stream = recognizer.createStream()
    stream.acceptWaveform({ sampleRate: 16000, samples })
    recognizer.decode(stream)
    const raw = recognizer.getResult(stream)
    const text = String((raw && raw.text) || '').trim()
    if (!text) return
    report({ event: 'text', text, seconds, took: (Date.now() - started) / 1000 })
  }

  const rl = createInterface({ input: process.stdin })
  rl.on('line', (line) => {
    const trimmed = line.trim()
    if (!trimmed) return
    let payload = null
    try { payload = JSON.parse(trimmed) } catch (error) { return }
    if (payload.stop) { rl.close(); return }
    if (payload.reset) { vad.clear(); speaking = false; return }
    const raw = String(payload.pcm || '')
    if (!raw) return
    const buffer = Buffer.from(raw, 'base64')
    const count = Math.floor(buffer.length / 2)
    if (!count) return
    const samples = new Float32Array(count)
    for (let i = 0; i < count; i += 1) samples[i] = buffer.readInt16LE(i * 2) / 32768
    vad.acceptWaveform(samples)
    if (!speaking && vad.isDetected()) {
      speaking = true
      report({ event: 'speech_start' })
    }
    // 一段说完（可能因为静音、也可能因为说太久被切开）就转写
    if (!vad.isEmpty()) flush()
  })
  rl.on('close', () => process.exit(0))
}

const args = process.argv.slice(2)
if (args.includes('--serve')) {
  try {
    serve()
  } catch (error) {
    report({ event: 'error', error: error && error.message ? error.message : String(error) })
    process.exit(1)
  }
} else {
  console.error('用法：node rhema.mjs --serve')
  process.exit(2)
}
