import { useEffect, useRef, useState } from 'react'

const STATUS_TEXT = { pending: '待处理', running: '领取中', done: '已复核' }

function fmtTime(value) {
  if (!value) return ''
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

export default function App() {
  const [username, setUsername] = useState('printer')
  const [password, setPassword] = useState('print123456')
  const [token, setToken] = useState(localStorage.getItem('print_token') || '')
  const [role, setRole] = useState(localStorage.getItem('print_role') || '')
  const [tab, setTab] = useState('jobs')
  const [rows, setRows] = useState([])
  const [logs, setLogs] = useState([])
  const [timeoutSeconds, setTimeoutSeconds] = useState(60)
  const [timeoutInput, setTimeoutInput] = useState('60')
  const editingTimeout = useRef(false)
  const [sheet, setSheet] = useState('插页-02')
  const [cyan, setCyan] = useState('0.08')
  const [magenta, setMagenta] = useState('0.02')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [, setTick] = useState(0)

  const isWriter = role === 'writer'

  async function api(path, options = {}) {
    const res = await fetch(path, {
      ...options,
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(data.detail || '请求失败')
    return data
  }

  async function load() {
    const [jobRows, setting, logRows] = await Promise.all([
      api('/api/jobs'),
      api('/api/claim-timeout'),
      api('/api/reclaim-logs'),
    ])
    setRows(jobRows)
    setTimeoutSeconds(setting.timeout_seconds)
    if (!editingTimeout.current) setTimeoutInput(String(setting.timeout_seconds))
    setLogs(logRows)
    setTick((n) => n + 1)
  }

  useEffect(() => {
    if (!token) return
    load().catch((err) => setError(err.message))
    const timer = setInterval(() => {
      load().catch((err) => setError(err.message))
    }, 1000)
    return () => clearInterval(timer)
  }, [token])

  async function enter() {
    setError('')
    try {
      const data = await api('/api/auth/login', {
        method: 'POST',
        body: JSON.stringify({ username, password }),
      })
      localStorage.setItem('print_token', data.access_token)
      localStorage.setItem('print_role', data.role)
      setToken(data.access_token)
      setRole(data.role)
    } catch (err) {
      setError(err.message)
    }
  }

  async function send() {
    setError('')
    try {
      await api('/api/jobs', {
        method: 'POST',
        body: JSON.stringify({
          sheet,
          cyan_mm: Number(cyan),
          magenta_mm: Number(magenta),
        }),
      })
    } catch (err) {
      setError(err.message)
    }
  }

  async function saveTimeout() {
    setError('')
    setNotice('')
    const value = Number(timeoutInput)
    if (!Number.isInteger(value) || value < 1) {
      setError('超时秒数须为不小于 1 的整数')
      return
    }
    try {
      const data = await api('/api/claim-timeout', {
        method: 'PUT',
        body: JSON.stringify({ timeout_seconds: value }),
      })
      setTimeoutSeconds(data.timeout_seconds)
      setTimeoutInput(String(data.timeout_seconds))
      setNotice(`已保存：${data.timeout_seconds} 秒，只约束此后新落入领取态的任务`)
    } catch (err) {
      setError(err.message)
    }
  }

  async function stallClaim() {
    setError('')
    try {
      await api('/api/jobs/stall', {
        method: 'POST',
        body: JSON.stringify({
          sheet: `拖住演示-${new Date().toLocaleTimeString('zh-CN', { hour12: false })}`,
          cyan_mm: 0.1,
          magenta_mm: 0.1,
        }),
      })
      await load()
    } catch (err) {
      setError(err.message)
    }
  }

  function leave() {
    localStorage.clear()
    setToken('')
    setRole('')
  }

  if (!token) {
    return (
      <main>
        <h1>印刷套准复核台</h1>
        <p>提交后接口只入队。另一进程领走偏差并写结论，页面轮询到结论出现。</p>
        <input value={username} onChange={(e) => setUsername(e.target.value)} />
        <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
        <button onClick={enter}>登录</button>
        {error && <p>{error}</p>}
        <p>printer / print123456 可送复核；checker / check123456 只看</p>
      </main>
    )
  }

  const running = rows.filter((row) => row.status === 'running')

  return (
    <main>
      <h1>印刷套准复核台</h1>
      <nav style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
        <button onClick={() => setTab('jobs')} disabled={tab === 'jobs'}>任务</button>
        <button onClick={() => setTab('timeout')} disabled={tab === 'timeout'}>领取超时</button>
        <span style={{ marginLeft: 'auto' }}>
          {username}（{isWriter ? '印刷员' : '只读'}）<button onClick={leave}>退出</button>
        </span>
      </nav>
      {error && <p style={{ color: 'crimson' }}>{error}</p>}
      {notice && <p style={{ color: 'seagreen' }}>{notice}</p>}

      {tab === 'jobs' && (
        <section>
          {isWriter && (
            <p>
              <input value={sheet} onChange={(e) => setSheet(e.target.value)} />
              <input value={cyan} onChange={(e) => setCyan(e.target.value)} />
              <input value={magenta} onChange={(e) => setMagenta(e.target.value)} />
              <button onClick={send}>送复核</button>
            </p>
          )}
          <table border={1} cellPadding={4}>
            <thead>
              <tr><th>印张</th><th>青</th><th>品</th><th>状态</th><th>结论</th><th>回收次数</th></tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td>{row.sheet}</td>
                  <td>{row.cyan_mm}</td>
                  <td>{row.magenta_mm}</td>
                  <td>{STATUS_TEXT[row.status] || row.status}</td>
                  <td>{row.verdict || '等待'}</td>
                  <td>{row.reclaim_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {tab === 'timeout' && (
        <section>
          <h2>超时秒数设置</h2>
          {isWriter ? (
            <p>
              领取超过
              <input
                value={timeoutInput}
                onChange={(e) => setTimeoutInput(e.target.value)}
                onFocus={() => { editingTimeout.current = true }}
                onBlur={() => { editingTimeout.current = false }}
                style={{ width: 80, margin: '0 6px' }}
              />
              秒未写出结论即自动退回待处理。
              <button onClick={saveTimeout} style={{ marginLeft: 8 }}>保存</button>
            </p>
          ) : (
            <p>当前超时设置：<strong>{timeoutSeconds} 秒</strong>（只读账号可查看，不可修改）</p>
          )}
          <p>改秒数只约束此后新落入领取态的任务，已在领取中的任务仍按领取时的秒数计时。</p>

          <h2>领取中监视</h2>
          {isWriter && (
            <p>
              <button onClick={stallClaim}>模拟拖住一条领取</button>
              （任务落入领取态但不写结论，超时后应自动退回待处理并出现回收流水）
            </p>
          )}
          {running.length === 0 ? (
            <p>当前没有领取中的任务。</p>
          ) : (
            <table border={1} cellPadding={4}>
              <thead>
                <tr><th>印张</th><th>领取时刻</th><th>超时秒数</th><th>剩余</th></tr>
              </thead>
              <tbody>
                {running.map((row) => {
                  const deadline = new Date(row.claimed_at).getTime() + row.timeout_seconds * 1000
                  const remainMs = deadline - Date.now()
                  return (
                    <tr key={row.id}>
                      <td>{row.sheet}</td>
                      <td>{fmtTime(row.claimed_at)}</td>
                      <td>{row.timeout_seconds}</td>
                      <td style={{ color: remainMs <= 0 ? 'crimson' : 'inherit' }}>
                        {remainMs <= 0 ? '已超时，等待回收' : `${Math.ceil(remainMs / 1000)} 秒`}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}

          <h2>回收流水</h2>
          {logs.length === 0 ? (
            <p>暂无回收记录。</p>
          ) : (
            <table border={1} cellPadding={4}>
              <thead>
                <tr><th>回收时刻</th><th>印张</th><th>任务</th><th>领取时刻</th><th>超时秒数</th><th>第几次回收</th></tr>
              </thead>
              <tbody>
                {logs.map((log) => (
                  <tr key={log.id}>
                    <td>{fmtTime(log.reclaimed_at)}</td>
                    <td>{log.sheet}</td>
                    <td>#{log.job_id}</td>
                    <td>{fmtTime(log.claimed_at)}</td>
                    <td>{log.timeout_seconds}</td>
                    <td>第 {log.reclaim_no} 次</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      )}
    </main>
  )
}
