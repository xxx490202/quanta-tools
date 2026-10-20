// Quanta 生成的文件（应用每次启动会按模板覆盖，改这里没用）。
//
// 给「子智能体的 preset」收窄工具：apply 跑在这个 preset 被挂上的那个 scope 里，
// ctx.tools.restrict({ allow }) 就只作用在这个孩子身上（全局工具仍然全局，只是孩子
// 看不到没 allow 的那些）。
//
// allow 里可能有些名字在这一版 dsh / 这个组合里压根没有（比如某个 MCP 工具没挂）——
// restrict 遇到不认识的名字会整条拒绝，所以这里从它的报错里把「known global tools」
// 读出来，取交集再试一次；一个全局工具都留不下（角色只勾了 MCP 工具那种）就把全局工具
// 全部 deny 掉，只留会话自己的那些。
const MARK = 'QUANTA-PRESET-TOOLS '
const name = 'quanta-preset-tools'
const inject = ['tools']

function note(payload) {
  try { process.stderr.write(MARK + JSON.stringify(payload) + '\n') } catch (error) { /* 忽略 */ }
}

function knownFromError(error) {
  const text = String((error && error.message) || error || '')
  const at = text.indexOf('known global tools:')
  if (at < 0) return null
  const names = text.slice(at + 'known global tools:'.length)
    .split(',').map((one) => one.trim().replace(/^"|"$/g, '')).filter(Boolean)
  return names.length ? new Set(names) : null
}

function apply(ctx, config) {
  const wanted = Array.isArray(config && config.allow)
    ? [...new Set(config.allow.map((one) => String(one)).filter(Boolean))] : []
  if (!wanted.length) return
  try {
    ctx.tools.restrict({ allow: wanted })
    note({ ok: true, allow: wanted })
  } catch (error) {
    const text = String((error && error.message) || error)
    // preset 定义刚加载时还没有 agent scope，restrict 会拒绝——这是正常的，
    // 真挂到孩子身上时还会再跑一次，那次才作数；这种预期内的错误不写日志
    if (text.includes('requires a scoped context')) return
    const known = knownFromError(error)
    const usable = known ? wanted.filter((one) => known.has(one)) : []
    if (!known) {
      note({ ok: false, wanted, error: String((error && error.message) || error) })
      return
    }
    if (!usable.length) {
      // 一个全局工具都不留（比如角色只勾了 MCP 工具）：把所有全局工具 deny 掉，
      // 会话自己的那些（MCP）不受影响——这比「退回工具不限」更接近用户勾的那份名单
      try {
        ctx.tools.restrict({ deny: [...known] })
        note({ ok: true, allow: [], denied: [...known], dropped: wanted })
      } catch (inner) {
        note({ ok: false, wanted, error: String((inner && inner.message) || inner) })
      }
      return
    }
    try {
      ctx.tools.restrict({ allow: usable })
      note({ ok: true, allow: usable, dropped: wanted.filter((one) => !known.has(one)) })
    } catch (inner) {
      note({ ok: false, wanted, error: String((inner && inner.message) || inner) })
    }
  }
}

export { apply, inject, name }
