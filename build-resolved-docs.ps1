#requires -Version 5.1
<#
.SYNOPSIS
    Generate the resolved NCS 1.6.1 docs index (ncs-1.6.1-resolved.sqlite).

.DESCRIPTION
    Orchestrates the gated build described in docs/build-ncs-1.6.1-doc.md:

      1. docker build  - the pinned toolchain image (doxygen 1.8.13, py3.8).
      2. docker run    - west-clones NCS v1.6.1 fresh and renders ALL docsets to
                         resolved HTML under <ScratchRoot>\out\_build\html.
      3. build_index   - ingests that HTML (--format html) into the committed
                         sdk-docs-mcp\ncs-1.6.1-resolved.sqlite (runs on the host
                         via uv; reuses the cached embedding model).

    Each stage is independently skippable so you can resume after a failure or
    re-index without rebuilding HTML. Sources are cloned inside the container -
    no local C:\ncs\v1.6.1 workspace is required, and nothing in the repo or that
    workspace is modified.

.PARAMETER ScratchRoot
    Writable scratch dir (NOT under the repo) for the west workspace + _build.
    Needs ~3-5 GB. Default: C:\ncs-docbuild.

.PARAMETER Rev
    NCS manifest revision to clone. Default: v1.6.1.

.PARAMETER Targets
    Optional ninja targets to build instead of all docsets, e.g.
    -Targets nrf-html-all,nrfxlib-html-all  (each carries its own deps).

.PARAMETER SkipImageBuild
    Reuse an existing 'ncs161-docs' image.

.PARAMETER SkipDocsBuild
    Skip the container build; reuse HTML already under <ScratchRoot>\out.

.PARAMETER SkipIndex
    Build only the HTML; don't (re)generate the sqlite index.

.EXAMPLE
    .\build-resolved-docs.ps1
    Full run: image, all-docset HTML, and the index.

.EXAMPLE
    .\build-resolved-docs.ps1 -SkipImageBuild -Targets nrf-html-all,nrfxlib-html-all
    Reuse the image; build only the API-critical docsets, then index.

.EXAMPLE
    .\build-resolved-docs.ps1 -SkipImageBuild -SkipDocsBuild
    HTML already built - just (re)generate the index.
#>
[CmdletBinding()]
param(
    [string]   $ScratchRoot = 'C:\ncs-docbuild',
    [string]   $Rev         = 'v1.6.1',
    [string]   $ImageTag    = 'ncs161-docs',
    [string[]] $Targets,
    [switch]   $SkipImageBuild,
    [switch]   $SkipDocsBuild,
    [switch]   $SkipIndex
)

$ErrorActionPreference = 'Stop'
$RepoRoot = $PSScriptRoot

# Docker Desktop wants forward-slash, drive-lettered mount paths (C:/foo:/bar).
function ConvertTo-DockerPath([string] $p) {
    return ((Resolve-Path -LiteralPath $p).Path -replace '\\', '/')
}

function Assert-Tool([string] $name) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        throw "'$name' was not found on PATH. Install it and re-run."
    }
}

function Invoke-Checked {
    param([string] $What, [scriptblock] $Action)
    Write-Host "==> $What" -ForegroundColor Cyan
    & $Action
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit $LASTEXITCODE)." }
}

# --- preflight -------------------------------------------------------------
Assert-Tool docker
if (-not $SkipIndex) { Assert-Tool uv }

$dockerDir = Join-Path $RepoRoot 'docker'
$dockerfile = Join-Path $dockerDir 'ncs-1.6.1-docs.Dockerfile'
if (-not (Test-Path $dockerfile)) { throw "Dockerfile not found: $dockerfile" }

$srcDir = Join-Path $ScratchRoot 'src'
$outDir = Join-Path $ScratchRoot 'out'
New-Item -ItemType Directory -Force -Path $srcDir, $outDir | Out-Null

$htmlRoot  = Join-Path $outDir '_build\html'
$indexPath = Join-Path $RepoRoot 'sdk-docs-mcp\ncs-1.6.1-resolved.sqlite'

Write-Host "Repo:        $RepoRoot"
Write-Host "Scratch src: $srcDir"
Write-Host "Scratch out: $outDir"
Write-Host "NCS rev:     $Rev"
if ($Targets) { Write-Host "Targets:     $($Targets -join ', ')" }
Write-Host ''

# --- 1) image --------------------------------------------------------------
if ($SkipImageBuild) {
    Write-Host "==> Skipping image build (reusing '$ImageTag')." -ForegroundColor DarkYellow
} else {
    Invoke-Checked "docker build $ImageTag (confirm doxygen 1.8.13 in the log)" {
        docker build -t $ImageTag -f (ConvertTo-DockerPath $dockerfile) (ConvertTo-DockerPath $dockerDir)
    }
}

# --- 2) resolved HTML (in container) ---------------------------------------
if ($SkipDocsBuild) {
    Write-Host "==> Skipping HTML build (reusing $htmlRoot)." -ForegroundColor DarkYellow
} else {
    $runArgs = @(
        'run', '--rm',
        '-e', "NCS_REV=$Rev",
        '-v', "$(ConvertTo-DockerPath $dockerDir):/work:ro",
        '-v', "$(ConvertTo-DockerPath $srcDir):/src",
        '-v', "$(ConvertTo-DockerPath $outDir):/out",
        $ImageTag
    )
    if ($Targets) {
        $runArgs += @('bash', '/work/build-docs.sh')
        foreach ($t in $Targets) { $runArgs += @('--target', $t) }
    }
    Invoke-Checked 'docker run - west clone + ninja build (30-90 min)' {
        docker @runArgs
    }
}

# Sanity: a former doxygen stub should now carry real signatures.
$stub = Join-Path $htmlRoot 'nrf\security\secure_services.html'
if (Test-Path $stub) {
    if (Select-String -Path $stub -Pattern 'spm_request' -Quiet) {
        Write-Host "==> Verified: resolved API signatures present in secure_services.html" -ForegroundColor Green
    } else {
        Write-Warning "secure_services.html exists but no 'spm_request' signature found - inspect the HTML."
    }
} elseif (-not (Test-Path $htmlRoot)) {
    throw "No HTML at $htmlRoot - the docs build did not produce output."
}

# --- 3) resolved index (on host) -------------------------------------------
if ($SkipIndex) {
    Write-Host "==> Skipping index build. HTML is under $htmlRoot." -ForegroundColor DarkYellow
    return
}

Push-Location $RepoRoot
try {
    Invoke-Checked 'build_index.py --format html (~45 min; first run caches the model)' {
        # --source-root is the west clone the HTML was built from (commit-exact
        # NCS v1.6.1): citations map each rendered page back to its source there,
        # and get_doc serves it. No committed snapshot involved.
        uv run --project sdk-docs-mcp python -u sdk-docs-mcp/build_index.py `
            --format html `
            --docs (ConvertTo-DockerPath $htmlRoot) `
            --source-root (ConvertTo-DockerPath $srcDir) `
            --out sdk-docs-mcp/ncs-1.6.1-resolved.sqlite
    }
} finally {
    Pop-Location
}

# --- summary ---------------------------------------------------------------
if (Test-Path $indexPath) {
    $mb = [math]::Round((Get-Item $indexPath).Length / 1MB, 1)
    Write-Host ''
    Write-Host "DONE - $indexPath ($mb MB)" -ForegroundColor Green
    try {
        # NB: a here-string terminator must start at column 0, even when nested.
        $py = @'
import sqlite3, sys
m = dict(sqlite3.connect(sys.argv[1]).execute("SELECT key, value FROM meta").fetchall())
for k in ("section_count", "link_count", "source_format", "docs_root_relative"):
    print("  %-13s: %s" % (k, m.get(k)))
'@
        $py | uv run --project sdk-docs-mcp python - $indexPath
    } catch { Write-Warning "Could not read index meta: $_" }
    Write-Host ''
    Write-Host "Next: reload MCP servers in Claude Code, then verify per docs/build-ncs-1.6.1-doc.md."
    Write-Host "      To commit the artifact:  git add $indexPath ; git commit -m 'Add resolved NCS 1.6.1 index'"
} else {
    throw "Index build reported success but $indexPath is missing."
}
