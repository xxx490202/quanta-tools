// 识别（STT）引擎表：语音输入（asr.mjs）和 Rhema 的监听（rhema.mjs）共用这一份。
//
// 换引擎只换「哪个模型 + 怎么配」，断句用的 silero VAD 和调用流程都不动。模型都在
// <数据目录>/voice/<引擎目录>/ 下；VAD 是公共的一份（<数据目录>/voice/vad/）。
//
// 选哪个：环境变量 QUANTA_ASR_ENGINE（界面上那个选择器由桥传进来）。
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)
export const ASR_ENGINES = {
}

// Qwen3-ASR 1.7B（MLX）：跑在 Apple GPU 上的大模型（1.7B），更准、自带标点；
// 代价是模型 2.3GB、一句约 1~1.4 秒。运行时是 **python 的 mlx-audio**（不是 sherpa），
// 所以这条引擎交给 connectors/voice/mlx_asr.py（协议和其它引擎一模一样）。
ASR_ENGINES['qwen3-mlx'] = {
  label: 'Qwen3-ASR 1.7B (MLX)',
  dir: 'qwen3-asr-mlx',
  runtime: 'python',
  script: 'mlx_asr.py',
  note: '最大最准（2.3GB，Apple GPU）：带标点，一句约 1 秒',
  files: ['model.safetensors', 'config.json', 'vocab.json', 'merges.txt',
          'tokenizer_config.json'],
  config: () => ({}),
  tokens: () => '',
  vad: () => null,
}

// 同一个模型的 0.6B 小号（MLX 8bit，964MB）：对话那种短句场景更划算，准确率略降
ASR_ENGINES['qwen3-mlx-0.6b'] = {
  label: 'Qwen3-ASR 0.6B (MLX)',
  dir: 'qwen3-0.6b-mlx',
  runtime: 'python',
  script: 'mlx_asr.py',
  note: '同门小号（0.96GB）：更快，准确率略低于 1.7B',
  repo: 'mlx-community/Qwen3-ASR-0.6B-8bit',
  assets: [
    { name: 'model.safetensors', bytes: 1006229426 },
    { name: 'vocab.json', bytes: 2776833 },
    { name: 'merges.txt', bytes: 1671853 },
    { name: 'tokenizer_config.json', bytes: 12487 },
    { name: 'config.json', bytes: 7187 },
  ],
  files: ['model.safetensors', 'config.json', 'vocab.json', 'merges.txt',
          'tokenizer_config.json'],
  config: () => ({}),
  tokens: () => '',
  vad: () => null,
}

export const ASR_KEYS = Object.keys(ASR_ENGINES)

// 跑 mlx 要用装了 mlx-audio 的那个 python：挨个试，挑第一个 import 得动的。
// （应用主进程找 python 也是这个路子：从访达启动时 PATH 很干净，不能只信 python3）
export function pythonForMlx() {
  const fs = require('node:fs')
  const { execFileSync } = require('node:child_process')
  const candidates = [
    (process.env.QUANTA_PYTHON || '').trim(),
    '/opt/anaconda3/bin/python3',
    '/usr/local/bin/python3',
    '/opt/homebrew/bin/python3',
    'python3',
  ].filter(Boolean)
  for (const bin of candidates) {
    try {
      if (bin.includes('/') && !fs.existsSync(bin)) continue
      execFileSync(bin, ['-c', 'import mlx_audio'], { stdio: 'ignore', timeout: 8000 })
      return bin
    } catch (error) { /* 换下一个 */ }
  }
  return ''
}

// 默认用哪个引擎（环境变量没给 / 给了个不认识的名字时用这个）。
// **必须是表里有的键**：以前这里写死 'sensevoice'，SenseVoice 删掉之后，只要有一次
// 调用没带引擎（界面有几处 voice.status 就不带），侧车一进来就
// `ASR_ENGINES[ENGINE].runtime` 崩掉，界面上显示成
// 「Voice input is not ready: Node.js v24.15.0」（用户报过）。
export const DEFAULT_ASR_ENGINE = 'qwen3-mlx-0.6b'

export function asrEngine() {
  const want = (process.env.QUANTA_ASR_ENGINE || '').trim()
  if (Object.prototype.hasOwnProperty.call(ASR_ENGINES, want)) return want
  if (Object.prototype.hasOwnProperty.call(ASR_ENGINES, DEFAULT_ASR_ENGINE)) return DEFAULT_ASR_ENGINE
  return ASR_KEYS[0]              // 连默认那个也被删了：表里第一个，总之别崩
}

// 断句用的 silero VAD：**公共的一份**（以前借住在 sensevoice 那个目录里，
// 删掉 SenseVoice 之后挪到 voice/vad/ 下，所有引擎共用）。下载清单见 asr.mjs 的 VAD_ASSET
export function vadPath() {
  return dataDirPath() + '/voice/vad/silero_vad.onnx'
}

function dataDirPath() {
  const env = (process.env.QUANTA_DATA_DIR || '').trim()
  if (env) return env
  return require('node:os').homedir() + '/Library/Application Support/Quantification'
}

export const VAD_ASSET = {
  name: 'silero_vad.onnx',
  dir: 'vad',
  bytes: 1807522,
  sha256: 'a35ebf52fd3ce5f1469b2a36158dba761bc47b973ea3382b3186ca15b1f5af28',
  url: 'https://huggingface.co/csukuangfj/vad/resolve/fba88cd2e921609e7675c3aaf51e0d9b295da4bc/silero_vad.onnx',
}
