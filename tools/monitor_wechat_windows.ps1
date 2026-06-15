param(
    [int]$Seconds = 600,
    [int]$IntervalSeconds = 1,
    [string]$OutputPath = ""
)

$ErrorActionPreference = "SilentlyContinue"

if (-not $OutputPath) {
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $OutputPath = Join-Path (Get-Location) "logs\wechat_vscode_monitor_$stamp.jsonl"
}

$outDir = Split-Path -Parent $OutputPath
if ($outDir) {
    New-Item -ItemType Directory -Force -Path $outDir | Out-Null
}

Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class WeChatWindowMonitorWin32 {
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern int GetWindowTextLengthW(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern int GetWindowTextW(IntPtr hWnd, StringBuilder text, int count);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out int processId);
  public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
}
"@ | Out-Null

function Get-WindowTitle([IntPtr]$Handle) {
    $len = [WeChatWindowMonitorWin32]::GetWindowTextLengthW($Handle)
    $sb = New-Object System.Text.StringBuilder($len + 2)
    [WeChatWindowMonitorWin32]::GetWindowTextW($Handle, $sb, $sb.Capacity) | Out-Null
    return $sb.ToString()
}

function Get-WindowProcessId([IntPtr]$Handle) {
    $windowPid = 0
    [WeChatWindowMonitorWin32]::GetWindowThreadProcessId($Handle, [ref]$windowPid) | Out-Null
    return $windowPid
}

function New-Snapshot {
    $wechatWindows = New-Object System.Collections.ArrayList
    $callback = [WeChatWindowMonitorWin32+EnumWindowsProc]{
        param([IntPtr]$hWnd, [IntPtr]$lParam)
        $windowPid = Get-WindowProcessId $hWnd
        try {
            $proc = Get-Process -Id $windowPid -ErrorAction Stop
        } catch {
            return $true
        }
        if ($proc.ProcessName -notin @("Weixin", "WeChat", "WeChatAppEx")) {
            return $true
        }
        $rect = New-Object WeChatWindowMonitorWin32+RECT
        [WeChatWindowMonitorWin32]::GetWindowRect($hWnd, [ref]$rect) | Out-Null
        [void]$wechatWindows.Add([ordered]@{
            handle = $hWnd.ToInt64()
            pid = $windowPid
            process = $proc.ProcessName
            title = Get-WindowTitle $hWnd
            visible = [WeChatWindowMonitorWin32]::IsWindowVisible($hWnd)
            iconic = [WeChatWindowMonitorWin32]::IsIconic($hWnd)
            left = $rect.Left
            top = $rect.Top
            width = $rect.Right - $rect.Left
            height = $rect.Bottom - $rect.Top
        })
        return $true
    }
    [WeChatWindowMonitorWin32]::EnumWindows($callback, [IntPtr]::Zero) | Out-Null

    $fg = [WeChatWindowMonitorWin32]::GetForegroundWindow()
    $fgPid = Get-WindowProcessId $fg
    $fgProc = ""
    try {
        $fgProc = (Get-Process -Id $fgPid -ErrorAction Stop).ProcessName
    } catch {}

    $latestDebug = Get-ChildItem "$env:TEMP\wechat_foreground_debug" -File |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    $latestPipeline = Get-ChildItem "logs\pipeline-*.log" -File |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    $python = Get-Process python -ErrorAction SilentlyContinue |
        Select-Object Id, ProcessName, StartTime, CPU, Path

    return [ordered]@{
        ts = (Get-Date).ToString("o")
        foreground = [ordered]@{
            handle = $fg.ToInt64()
            pid = $fgPid
            process = $fgProc
            title = Get-WindowTitle $fg
        }
        wechat_windows = @($wechatWindows)
        python = @($python)
        latest_debug = if ($latestDebug) {
            [ordered]@{
                name = $latestDebug.Name
                time = $latestDebug.LastWriteTime.ToString("o")
                bytes = $latestDebug.Length
            }
        } else { $null }
        latest_pipeline_log = if ($latestPipeline) {
            [ordered]@{
                name = $latestPipeline.Name
                time = $latestPipeline.LastWriteTime.ToString("o")
                bytes = $latestPipeline.Length
            }
        } else { $null }
    }
}

$deadline = (Get-Date).AddSeconds($Seconds)
while ((Get-Date) -lt $deadline) {
    (New-Snapshot | ConvertTo-Json -Depth 8 -Compress) | Add-Content -Encoding UTF8 $OutputPath
    Start-Sleep -Seconds $IntervalSeconds
}

Write-Output $OutputPath
