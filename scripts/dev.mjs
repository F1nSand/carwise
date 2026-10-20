import { existsSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { spawn } from 'node:child_process'
import process from 'node:process'

if (Number(process.versions.node.split('.')[0]) < 22) throw new Error('请使用 Node.js 22 或更高版本。')
const root = resolve(import.meta.dirname, '..')
const frontend = join(root, 'frontend')
const isWindows = process.platform === 'win32'
const python = join(root, '.venv', isWindows ? 'Scripts' : 'bin', isWindows ? 'python.exe' : 'python')
const npm = isWindows ? 'npm.cmd' : 'npm'

if (!existsSync(python) || !existsSync(join(frontend, 'node_modules'))) {
  console.error('依赖尚未安装，请先在项目根目录执行：npm run setup')
  process.exit(1)
}

const children = []
let shuttingDown = false

function start(label, command, args, cwd) {
  console.log(`[${label}] ${command} ${args.join(' ')}`)
  const child = spawn(command, args, {
    cwd,
    stdio: 'inherit',
    windowsHide: false,
    shell: command === npm,
  })
  child.on('error', (error) => {
    console.error(`[${label}] 启动失败：${error.message}`)
    shutdown(1)
  })
  child.on('exit', (code, signal) => {
    if (!shuttingDown) {
      console.error(`[${label}] 已退出：${signal || `code ${code ?? 1}`}`)
      shutdown(code ?? 1)
    }
  })
  children.push(child)
}

function shutdown(code = 0) {
  if (shuttingDown) return
  shuttingDown = true
  for (const child of children) {
    if (!child.killed) child.kill('SIGINT')
  }
  setTimeout(() => process.exit(code), 800)
}

process.once('SIGINT', () => shutdown(0))
process.once('SIGTERM', () => shutdown(0))

console.log('=== 二手车智能分析开发服务 ===')
start('backend', python, ['-m', 'uvicorn', 'backend.service:app', '--host', '127.0.0.1', '--port', '8000', '--reload', '--reload-dir', join(root, 'backend')], root)
start('frontend', npm, ['run', 'dev', '--', '--host', '127.0.0.1'], frontend)
console.log('\n前端：http://127.0.0.1:5173')
console.log('后端：http://127.0.0.1:8000')
console.log('按 Ctrl+C 同时停止前后端。')
