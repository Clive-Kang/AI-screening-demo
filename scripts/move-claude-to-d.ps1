# ============================================================
#  把 C:\Users\<你>\.claude 整体迁移到 D:\claude-data
#  做法：复制 → 校验 → 原名备份 → 建目录联接（Junction）
#
#  运行前提：所有 Claude Code 会话 / VSCode 窗口 / 终端里的 claude 都已关闭
#  运行方式：右键「用 PowerShell 运行」，或 powershell -ExecutionPolicy Bypass -File 本脚本
#
#  与设 CLAUDE_CONFIG_DIR 的区别：
#    本脚本不动 ~/.claude.json（它在 .claude 外面，1KB），
#    所以 mcpServers 等账户级配置、登录凭据都不受影响，无需重新登录。
# ============================================================

$ErrorActionPreference = 'Stop'

$Source = Join-Path $env:USERPROFILE '.claude'
$Target = 'D:\claude-data'
$Backup = Join-Path $env:USERPROFILE '.claude.old'

function Say($msg, $color = 'Cyan') { Write-Host $msg -ForegroundColor $color }

# ---------- 0) 前置检查 ----------
Say "`n[0/5] 前置检查"

if (-not (Test-Path $Source))        { throw "源目录不存在：$Source" }
if (-not (Test-Path 'D:\'))          { throw "D 盘不可用" }
if (Test-Path $Target)               { throw "目标已存在：$Target —— 请先改名或删除，避免覆盖" }
if (Test-Path $Backup)               { throw "备份名已占用：$Backup —— 上次迁移未清理，请先处理" }

$running = Get-Process -Name claude -ErrorAction SilentlyContinue
if ($running) {
    throw "检测到 $($running.Count) 个 claude 进程仍在运行（PID: $($running.Id -join ', ')）。请全部关闭后重跑。"
}
Say "  源目录 / 目标空闲 / 无 claude 进程  —— 通过" 'Green'

$srcCount = (Get-ChildItem $Source -Recurse -File -Force).Count
$srcSize  = (Get-ChildItem $Source -Recurse -File -Force | Measure-Object Length -Sum).Sum
Say ("  待迁移：{0} 个文件，{1:N1} MB" -f $srcCount, ($srcSize / 1MB))

# ---------- 1) 复制（不删源） ----------
Say "`n[1/5] 复制到 $Target（源目录保持不动）"
robocopy $Source $Target /E /COPY:DAT /R:1 /W:1 /NFL /NDL /NP | Out-Null
# robocopy 退出码 0-7 表示成功，>=8 才是真失败
if ($LASTEXITCODE -ge 8) { throw "robocopy 失败，退出码 $LASTEXITCODE" }
Say "  复制完成（robocopy 退出码 $LASTEXITCODE）" 'Green'

# ---------- 2) 校验 ----------
Say "`n[2/5] 校验文件数与体积"
$dstCount = (Get-ChildItem $Target -Recurse -File -Force).Count
$dstSize  = (Get-ChildItem $Target -Recurse -File -Force | Measure-Object Length -Sum).Sum
Say ("  源：{0} 个 / {1:N1} MB" -f $srcCount, ($srcSize / 1MB))
Say ("  目标：{0} 个 / {1:N1} MB" -f $dstCount, ($dstSize / 1MB))
if ($dstCount -ne $srcCount) {
    throw "文件数不一致（源 $srcCount vs 目标 $dstCount）。源目录未动，可安全重试。"
}
Say "  一致 —— 通过" 'Green'

# ---------- 3) 源目录改名备份 ----------
Say "`n[3/5] 原目录改名为 $Backup（此时尚未删除任何东西）"
Rename-Item -Path $Source -NewName (Split-Path $Backup -Leaf)
Say "  已改名" 'Green'

# ---------- 4) 建 Junction ----------
Say "`n[4/5] 建立目录联接 $Source -> $Target"
New-Item -ItemType Junction -Path $Source -Target $Target | Out-Null
Say "  已建立" 'Green'

# ---------- 5) 验证 ----------
Say "`n[5/5] 验证"
$link = Get-Item $Source -Force
Say ("  LinkType = {0}" -f $link.LinkType)
Say ("  Target   = {0}" -f $link.Target)

$probe = Join-Path $Source 'projects'
if (-not (Test-Path $probe)) { throw "验证失败：联接下看不到 projects 目录，请勿删除备份，手动排查" }
Say ("  projects 下可见 {0} 个项目目录：" -f (Get-ChildItem $probe -Directory).Count)
Get-ChildItem $probe -Directory | ForEach-Object { Say ("    - " + $_.Name) }

$mem = Join-Path $Source 'projects\d--ai-resume-screening-demo\memory'
if (Test-Path $mem) {
    Say ("  记忆文件可见 {0} 个" -f (Get-ChildItem $mem -File).Count)
}

Write-Host ""
Say "============================================" 'Green'
Say " 迁移完成" 'Green'
Say "============================================" 'Green'
Write-Host @"

下一步：
  1. 重新打开 VSCode / 终端，启动一个新的 Claude Code 会话
  2. 用 /memory 或 /context 确认记忆被正常加载
  3. 确认无误后，删掉备份释放 C 盘空间：
       Remove-Item "$Backup" -Recurse -Force

回滚（把一切还原到 C 盘原样）：
       cmd /c rmdir "$Source"        # 只删联接本身，不会跟进删 D 盘的真实文件
       Rename-Item "$Backup" ".claude"

注意：
  * 删联接务必用上面的 cmd /c rmdir。
    PowerShell 5.1 的 Remove-Item -Recurse 作用在联接上可能跟进删掉 D 盘真实数据。
  * ~/.claude-mem（claude-mem 插件的数据，9.3M）是独立目录，本次未搬迁。
  * 想把 projects 单独挪走而不是整个 .claude，改 $Target 与 $Source 即可，做法相同。
"@ -ForegroundColor Gray
