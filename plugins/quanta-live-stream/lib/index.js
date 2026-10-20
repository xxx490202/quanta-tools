// Quanta 生成的文件（应用每次启动会按模板覆盖，改这里没用）。
//
// 把模型**正在生成**的思考片段实时写到 stderr：应用那边的桥读 dsh 的 stderr，
// 就能把思考逐字画到界面上，而不是等整段想完才一次性冒出来。
//
// 顺带把「这一轮跑完没有」也写出去（agent/status 从 running 回到 idle 就是结算了）：
// dsh 自己起的那种轮次（后台任务完成通知塞回来、它又自己跑一轮）没有 ask 在等，
// 界面上那块「工作中 X 秒」就没人收尾——用户报过「后台任务好像不会停」。
//
// 再顺带把**后台任务**（job）的生死也写出去：应用输入框那个小图标靠它，
// 不写的话界面就不知道有没有任务在跑（job 事件在 dsh-jobs 里，见 ctx.jobs）。
//
// 上下文压缩（compaction/*）也在这里转一手：ACP 那条线**不报**压缩，界面上那条
// 「正在压缩上下文 → 上下文已自动压缩」的横线全靠这几行。
//
// 事件名和字段形状照 dsh 官方 headless profile 里同一路
// （agent/assistant-stream 的 frame.chunk.type === "reasoning-delta"）。
// 写不出去就算了——这条通道是锦上添花，绝不能把对话本身拖死。
const MARK = 'QUANTA-LIVE '
const name = 'quanta-live-stream'
const inject = ['jobs']

function apply(ctx) {
  const write = (payload) => {
    try { process.stderr.write(MARK + JSON.stringify(payload) + '\n') } catch (error) { /* 忽略 */ }
  }
  // 挂上了说一声（应用拿它当「插件到底有没有生效」的探针，不显示给用户）
  write({ boot: 1 })
  ctx.on('agent/assistant-stream', ({ frame }) => {
    try {
      const chunk = frame && frame.chunk
      if (!chunk || chunk.type !== 'reasoning-delta' || !chunk.text) return
      write({ t: chunk.text })
    } catch (error) { /* 忽略：见上 */ }
  })
  // 一轮的开始与结算（running / idle）：桥拿它给「自己起的那一轮」收尾
  ctx.on('agent/status', ({ status }) => {
    try {
      if (status !== 'running' && status !== 'idle') return
      write({ status: status })
    } catch (error) { /* 忽略：见上 */ }
  })
  // 后台任务：先报一次现在挂着的（应用重开时不用等新事件），再订阅增量的
  const pushJob = (event, job) => {
    const one = job || {}
    if (!one.id) return
    write({ job: { type: event, id: one.id, kind: one.kind, label: one.label,
                   status: one.status, detail: one.detail || '',
                   startedAt: one.startedAt, finishedAt: one.finishedAt } })
  }
  try {
    for (const one of (ctx.jobs.list() || [])) pushJob('registered', one)
    ctx.jobs.events.subscribe({ owners: 'scope' }, (event) => {
      try {
        if (!event || !event.type || event.type === 'output') return
        pushJob(event.type, event.job)
      } catch (error) { /* 忽略：见上 */ }
    })
  } catch (error) { /* 忽略：见上 */ }
  // 上下文压缩：自动的（模型干着活、上下文满了，dsh 在两步之间把老历史收成摘要——
  // 收完接着干）和手动的（/compact）都走这两条事件。桥拿它画对话栏里那条
  // 「正在压缩上下文 → 上下文已自动压缩」的横线。
  //   · start 是**开始摘要之前**就落盘的（那句话可能要跑十几秒），end 是换完历史落盘的；
  //   · turn 是数字＝自动（发生在某一轮中间），null＝手动（那一轮界面自己有路径，桥忽略）；
  //   · 省下多少 token 在 summary 那条上，按 compactionId 攒着，end 时一起报出去。
  const compactShadowed = new Map()
  ctx.on('session/event', (session, event) => {
    try {
      const type = event && event.type
      if (type !== 'compaction/start' && type !== 'compaction/summary'
          && type !== 'compaction/end') return
      const one = event.data || {}
      const id = String(one.compactionId || '')
      if (type === 'compaction/summary') {
        compactShadowed.set(id, Number(one.shadowedTokenCount) || 0)
        return
      }
      write({ compact: { phase: type === 'compaction/start' ? 'start' : 'end', id: id,
                         auto: typeof one.turn === 'number',
                         session: String((session && session.id) || ''),
                         shadowed: compactShadowed.get(id) || 0,
                         error: one.error ? 1 : 0 } })
      if (type === 'compaction/end') compactShadowed.delete(id)
    } catch (error) { /* 忽略：见上 */ }
  })
}

export { apply, inject, name }
