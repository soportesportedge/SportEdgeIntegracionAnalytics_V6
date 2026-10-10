param([ValidateSet('Start','Stop','Status','PauseCollector','ResumeCollector','PauseTelegram','ResumeTelegram','Remove')][string]$Action='Status')
$ErrorActionPreference='Stop'
$v6Root=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$v6Compose=Join-Path $v6Root 'compose.yaml'
switch($Action) {
  'Start' { docker compose -f $v6Compose up -d api inference-worker analytics }
  'Stop' { docker compose -f $v6Compose --profile collector --profile telegram stop }
  'Status' { docker compose -f $v6Compose --profile collector --profile telegram ps -a }
  'PauseCollector' { docker compose -f $v6Compose stop collector }
  'ResumeCollector' { docker compose -f $v6Compose --profile collector up -d collector }
  'PauseTelegram' { docker compose -f $v6Compose stop telegram-worker }
  'ResumeTelegram' { $env:V6_TELEGRAM_ENABLED='true'; docker compose -f $v6Compose --profile telegram up -d telegram-worker }
  # Removes only the NEW project. Its data volume is retained for recovery.
  'Remove' { docker compose -f $v6Compose --profile collector --profile telegram down }
}
if($LASTEXITCODE -ne 0){exit $LASTEXITCODE}
