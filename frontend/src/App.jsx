import { useEffect, useState } from 'react'

function TimeoutPage({ api, role }) {
  const [seconds, setSeconds] = useState(null)
  const [input, setInput] = useState('')
  const [claiming, setClaiming] = useState([])
  const [reclaims, setReclaims] = useState([])
  const [msg, setMsg] = useState('')

  async function load() {
    const [timeout, active, events] = await Promise.all([
      api('/api/claim-timeout'),
      api('/api/claiming'),
      api('/api/reclaims'),
    ])
    setSeconds(timeout.seconds)
    setInput((prev) => (prev === '' ? String(timeout.seconds) : prev))
    setClaiming(active)
    setReclaims(events)
  }

  useEffect(() => {
    load()
    const timer = setInterval(load, 1000)
    return () => clearInterval(timer)
  }, [])

  async function save() {
    setMsg('')
    const n = Number(input)
    if (!Number.isInteger(n) || n < 1) {
      setMsg('请输入正整数秒数')
      return
    }
    try {
      const data = await api('/api/claim-timeout', {
        method: 'PUT',
        body: JSON.stringify({ seconds: n }),
      })
      setSeconds(data.seconds)
      setMsg('已保存，新秒数只约束此后新落入领取态的任务')
      load()
    } catch (err) {
      setMsg(err.message)
    }
  }

  return (
    <section>
      <h2>领取超时</h2>

      <h3>超时秒数设置</h3>
      {role === 'writer' ? (
        <p>
          <input value={input} onChange={(e) => setInput(e.target.value)} />
          <button onClick={save}>保存秒数</button>
          当前 {seconds} 秒，只约束此后新落入领取态的任务
        </p>
      ) : (
        <p>当前 {seconds} 秒（只读账号可查看，不可修改）</p>
      )}
      {msg && <p>{msg}</p>}

      <h3>领取中监视</h3>
      <table>
        <thead>
          <tr><th>印张</th><th>领取时刻</th><th>已领取(秒)</th><th>超时(秒)</th><th>领取序号</th></tr>
        </thead>
        <tbody>
          {claiming.map((job) => (
            <tr key={job.id}>
              <td>{job.sheet}</td>
              <td>{new Date(job.claimed_at).toLocaleTimeString()}</td>
              <td>{job.elapsed_seconds}</td>
              <td>{job.claim_timeout_seconds}</td>
              <td>{job.claim_seq}</td>
            </tr>
          ))}
          {claiming.length === 0 && (
            <tr><td colSpan="5">当前没有领取中的任务</td></tr>
          )}
        </tbody>
      </table>

      <h3>回收流水</h3>
      <table>
        <thead>
          <tr><th>回收时间</th><th>印张</th><th>领取时刻</th><th>超时(秒)</th><th>领取序号</th></tr>
        </thead>
        <tbody>
          {reclaims.map((event) => (
            <tr key={event.id}>
              <td>{new Date(event.reclaimed_at).toLocaleTimeString()}</td>
              <td>{event.sheet}</td>
              <td>{new Date(event.claimed_at).toLocaleTimeString()}</td>
              <td>{event.timeout_seconds}</td>
              <td>{event.claim_seq}</td>
            </tr>
          ))}
          {reclaims.length === 0 && (
            <tr><td colSpan="5">暂无回收记录</td></tr>
          )}
        </tbody>
      </table>
    </section>
  )
}

export default function App() {
  const [username, setUsername] = useState('printer')
  const [password, setPassword] = useState('print123456')
  const [token, setToken] = useState(localStorage.getItem('print_token') || '')
  const [role, setRole] = useState(localStorage.getItem('print_role') || '')
  const [page, setPage] = useState('board')
  const [rows, setRows] = useState([])
  const [sheet, setSheet] = useState('插页-02')
  const [cyan, setCyan] = useState('0.08')
  const [magenta, setMagenta] = useState('0.02')
  const [error, setError] = useState('')

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
    setRows(await api('/api/jobs'))
  }

  useEffect(() => {
    if (!token) return
    load()
    const timer = setInterval(load, 1000)
    return () => clearInterval(timer)
  }, [token])

  async function enter() {
    const data = await api('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    })
    localStorage.setItem('print_token', data.access_token)
    localStorage.setItem('print_role', data.role)
    setToken(data.access_token)
    setRole(data.role)
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
        <p>printer / print123456 可送复核；checker / check123456 只看</p>
      </main>
    )
  }

  return (
    <main>
      <h1>印刷套准复核台</h1>
      <nav>
        <button onClick={() => setPage('board')} disabled={page === 'board'}>复核台</button>{' '}
        <button onClick={() => setPage('timeout')} disabled={page === 'timeout'}>领取超时</button>{' '}
        <button onClick={leave}>退出</button>
      </nav>
      {page === 'timeout' ? (
        <TimeoutPage api={api} role={role} />
      ) : (
        <section>
          {role === 'writer' && (
            <p>
              <input value={sheet} onChange={(e) => setSheet(e.target.value)} />
              <input value={cyan} onChange={(e) => setCyan(e.target.value)} />
              <input value={magenta} onChange={(e) => setMagenta(e.target.value)} />
              <button onClick={send}>送复核</button>
            </p>
          )}
          {error && <p>{error}</p>}
          <table>
            <thead>
              <tr><th>印张</th><th>青</th><th>品</th><th>状态</th><th>结论</th></tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td>{row.sheet}</td>
                  <td>{row.cyan_mm}</td>
                  <td>{row.magenta_mm}</td>
                  <td>{row.status}</td>
                  <td>{row.verdict || '等待'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </main>
  )
}
