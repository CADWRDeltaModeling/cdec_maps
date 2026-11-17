<#
.SYNOPSIS
    Create or update the scheduled Azure Container Apps Job that builds the
    CDEC cache, so the long download no longer runs inside a web container.

.DESCRIPTION
    The job runs the same image as the web app, mounts the same Azure Files share
    at /home/data, and runs run_cache_job.sh from the app directory on a cron
    schedule. It writes the Parquet cache (cdec_db/) that the web app reads, plus
    cdec_db/build_status.json (state, timings, counts). Writes are atomic and a
    lock file stops two builds overlapping, so the app can keep serving.

    Steps (idempotent; safe to re-run):
      1. Install the containerapp CLI extension and register providers.
      2. Create the Container Apps environment if it does not exist (shared by
         the CDEC and USGS jobs).
      3. Link the file share to the environment (uses the storage account key;
         Container Apps does not support identity-based Azure Files access).
      4. Create/update the job from a generated YAML. The registry password is
         placed in that temp file only for the call and the file is deleted.

    Use -WhatIf to print the YAML and the commands without changing anything.

.PARAMETER Cron
    Cron expression, evaluated in UTC. Default 10:00 UTC (02:00 PST).

.PARAMETER ReplicaTimeoutSeconds
    Hard limit for one run. Default 8 hours; the first full build is the long one,
    later runs are incremental.

.PARAMETER StartNow
    Start one execution immediately after creating/updating the job.

.EXAMPLE
    ./deploy_cache_job.ps1 -WhatIf
    ./deploy_cache_job.ps1 -StartNow
    az containerapp job execution list -g dwrbdo_dash_rg -n cdec-cache-build -o table
#>
[CmdletBinding()]
param(
    [string]$SubscriptionId = "c15db114-26b5-454c-b8f4-8a5eb5f16796",
    [string]$ResourceGroup = "dwrbdo_dash_rg",
    [string]$Location = "westus2",
    [string]$EnvironmentName = "dwrbdo-jobs-env",
    [string]$StorageAccountName = "dwrbdodashstore",
    [string]$FileShareName = "data",
    [string]$EnvStorageName = "datashare",
    [string]$JobName = "cdec-cache-build",
    [string]$RegistryName = "dashcr",
    [string]$Image = "dashcr.azurecr.io/conda-cdec_maps:latest",
    [string]$AppDir = "/home/data/cdec-maps",
    [string]$Cron = "0 10 * * *",
    [int]$ReplicaTimeoutSeconds = 28800,
    [int]$ReplicaRetryLimit = 0,
    [double]$Cpu = 1.0,
    [string]$Memory = "2Gi",
    [switch]$StartNow,
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
if (-not (Get-Command az -ErrorAction SilentlyContinue)) { throw "Azure CLI ('az') not found on PATH." }

function Invoke-Az {
    # Runs az. -ReadOnly calls always run (they change nothing); other calls are only
    # echoed under -WhatIf, with the storage key masked. -AllowFail returns $null on error.
    param([string[]]$AzArgs, [switch]$ReadOnly, [switch]$AllowFail)
    if ($WhatIf -and -not $ReadOnly) {
        $shown = for ($i = 0; $i -lt $AzArgs.Count; $i++) {
            if ($i -gt 0 -and $AzArgs[$i - 1] -eq "--azure-file-account-key") { "<redacted>" } else { $AzArgs[$i] }
        }
        Write-Host "[WhatIf] az $($shown -join ' ')" -ForegroundColor Yellow
        return
    }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { $out = & az @AzArgs --only-show-errors 2>$null } finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE -ne 0) {
        if ($AllowFail) { return $null }
        throw "az $($AzArgs[0..2] -join ' ') failed with exit code $LASTEXITCODE"
    }
    return $out
}

Write-Host "==> Preparing the Container Apps CLI extension and providers..." -ForegroundColor Cyan
# Installing the CLI extension only changes your local az installation, so it also runs under -WhatIf.
Invoke-Az -ReadOnly @("extension", "add", "--name", "containerapp", "--upgrade", "--yes") | Out-Null
foreach ($ns in "Microsoft.App", "Microsoft.OperationalInsights") {
    Invoke-Az @("provider", "register", "--namespace", $ns, "--subscription", $SubscriptionId) | Out-Null
}

Write-Host "==> Ensuring environment '$EnvironmentName'..." -ForegroundColor Cyan
$envId = Invoke-Az -ReadOnly -AllowFail @("containerapp", "env", "show", "-g", $ResourceGroup, "-n", $EnvironmentName,
    "--subscription", $SubscriptionId, "--query", "id", "-o", "tsv")
if (-not $envId) {
    Invoke-Az @("containerapp", "env", "create", "-g", $ResourceGroup, "-n", $EnvironmentName,
        "--location", $Location, "--subscription", $SubscriptionId) | Out-Null
    $envId = Invoke-Az -ReadOnly -AllowFail @("containerapp", "env", "show", "-g", $ResourceGroup, "-n", $EnvironmentName,
        "--subscription", $SubscriptionId, "--query", "id", "-o", "tsv")
}
if (-not $envId) { $envId = "<environment id, created by a real run>" }

Write-Host "==> Linking file share '$FileShareName' as '$EnvStorageName'..." -ForegroundColor Cyan
$storageKey = Invoke-Az -ReadOnly @("storage", "account", "keys", "list", "-g", $ResourceGroup,
    "-n", $StorageAccountName, "--subscription", $SubscriptionId, "--query", "[0].value", "-o", "tsv")
Invoke-Az @("containerapp", "env", "storage", "set", "-g", $ResourceGroup, "-n", $EnvironmentName,
    "--subscription", $SubscriptionId, "--storage-name", $EnvStorageName, "--access-mode", "ReadWrite",
    "--azure-file-account-name", $StorageAccountName, "--azure-file-account-key", $storageKey,
    "--azure-file-share-name", $FileShareName) | Out-Null

$registryServer = "$RegistryName.azurecr.io"
$registryUser = Invoke-Az -ReadOnly @("acr", "credential", "show", "-n", $RegistryName, "--subscription", $SubscriptionId,
    "--query", "username", "-o", "tsv")
$registryPassword = Invoke-Az -ReadOnly @("acr", "credential", "show", "-n", $RegistryName, "--subscription", $SubscriptionId,
    "--query", "passwords[0].value", "-o", "tsv")

function New-JobYaml {
    param([string]$Password)
    @"
location: $Location
properties:
  environmentId: $envId
  configuration:
    triggerType: Schedule
    replicaTimeout: $ReplicaTimeoutSeconds
    replicaRetryLimit: $ReplicaRetryLimit
    scheduleTriggerConfig:
      cronExpression: "$Cron"
      parallelism: 1
      replicaCompletionCount: 1
    secrets:
      - name: registry-password
        value: "$Password"
    registries:
      - server: $registryServer
        username: $registryUser
        passwordSecretRef: registry-password
  template:
    containers:
      - name: build
        image: $Image
        command: ["/bin/bash", "-c"]
        # The image puts its conda env at /env and its entrypoint (not used here) normally activates it.
        args: ["PATH=/env/bin:/usr/local/bin:/usr/bin:/bin bash $AppDir/run_cache_job.sh"]
        resources:
          cpu: $Cpu
          memory: $Memory
        volumeMounts:
          - volumeName: data
            mountPath: /home/data
    volumes:
      - name: data
        storageName: $EnvStorageName
        storageType: AzureFile
"@
}

if ($WhatIf) {
    Write-Host "==> Job definition (registry password redacted):" -ForegroundColor Cyan
    Write-Host (New-JobYaml -Password "<redacted>")
}

Write-Host "==> Creating/updating job '$JobName'..." -ForegroundColor Cyan
$exists = Invoke-Az -ReadOnly -AllowFail @("containerapp", "job", "show", "-g", $ResourceGroup, "-n", $JobName,
    "--subscription", $SubscriptionId, "--query", "name", "-o", "tsv")
$verb = if ($exists) { "update" } else { "create" }
$yamlPath = Join-Path ([IO.Path]::GetTempPath()) "$JobName-$([guid]::NewGuid().ToString('N')).yaml"
try {
    if (-not $WhatIf) { Set-Content -LiteralPath $yamlPath -Value (New-JobYaml -Password $registryPassword) -Encoding utf8 }
    Invoke-Az @("containerapp", "job", $verb, "-g", $ResourceGroup, "-n", $JobName,
        "--subscription", $SubscriptionId, "--yaml", $yamlPath) | Out-Null
}
finally {
    Remove-Item -LiteralPath $yamlPath -Force -ErrorAction SilentlyContinue
}

if ($StartNow) {
    Write-Host "==> Starting an execution..." -ForegroundColor Cyan
    Invoke-Az @("containerapp", "job", "start", "-g", $ResourceGroup, "-n", $JobName, "--subscription", $SubscriptionId) | Out-Null
}
Write-Host "==> Done. Status: az containerapp job execution list -g $ResourceGroup -n $JobName -o table" -ForegroundColor Green
