import { copyFileSync, existsSync, mkdirSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { spawnSync } from 'node:child_process'
import process from 'node:process'

if (Number(process.versions.node.split('.')[0]) < 22) throw new Error('请使用 Node.js 22 或更高版本。')
const root = resolve(import.meta.dirname, '..')
const frontend = join(root, 'frontend')
const venv = join(root, '.venv')
const isWindows = process.platform === 'win32'
const npm = isWindows ? 'npm.cmd' : 'npm'

function canRun(command, args = ['--version']) {
  const result = spawnSync(command, args, { stdio: 'ignore', windowsHide: true })
  return !result.error && result.status === 0
}

function run(command, args, cwd = root) {
  console.log(`\n> ${command} ${args.join(' ')}`)
  const result = spawnSync(command, args, {
    cwd,
    stdio: 'inherit',
    windowsHide: false,
    shell: command === npm,
  })
  if (result.error) throw result.error
  if (result.status !== 0) process.exit(result.status ?? 1)
}

function findPython() {
  if (process.env.PYTHON && canRun(process.env.PYTHON)) return { command: process.env.PYTHON, prefix: [] }
  if (isWindows && canRun('py', ['-3', '--version'])) return { command: 'py', prefix: ['-3'] }
  if (canRun('python', ['--version'])) return { command: 'python', prefix: [] }
  if (canRun('python3', ['--version'])) return { command: 'python3', prefix: [] }
  throw new Error('找不到 Python。请先安装 Python 3.12+，并加入 PATH。')
}

function venvPython() {
  return join(venv, isWindows ? 'Scripts' : 'bin', isWindows ? 'python.exe' : 'python')
}

console.log('=== 二手车智能分析：统一依赖安装 ===')
mkdirSync(venv, { recursive: true })
const basePython = findPython()
run(basePython.command, [...basePython.prefix, '-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else "需要 Python 3.12 或更高版本")'])
if (!existsSync(venvPython())) {
  run(basePython.command, [...basePython.prefix, '-m', 'venv', '.venv'])
}

run(venvPython(), ['-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else "现有 .venv 需要 Python 3.12 或更高版本")'])
run(venvPython(), ['-m', 'pip', 'install', '-r', 'requirements-dev.txt'])
run(npm, ['ci'], frontend)

const envFile = join(root, '.env')
const envExample = join(root, '.env.example')
if (!existsSync(envFile) && existsSync(envExample)) {
  copyFileSync(envExample, envFile)
  console.log('\n已创建 .env，请填入 DEEPSEEK_API_KEY 后再启动 Agent。')
}

console.log('\n依赖安装完成。启动命令：npm run dev')
