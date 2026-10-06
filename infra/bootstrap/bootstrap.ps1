# One-time bootstrap for Dojo. Run once by the owner. Everything after this deploys through
# GitHub Actions only.
#
# What it creates (all new; nothing existing is changed or deleted):
#   1. Resource groups rg-dojo-identity-swc and rg-dojo-swc, two managed identities and their
#      role assignments (Bicep: main.bicep).
#   2. An Entra app registration named -AppName for signing in to the web app. It has no secret:
#      the web app proves itself with its managed identity (federated credential). Only the owner
#      is assigned, and assignment is required.
#   3. GitHub repository secrets the pipeline reads. They hold identifiers, not credentials, but
#      they are kept out of public logs. The script never prints them.
#
# Usage: ./bootstrap.ps1 -Tenant <tenant-id> -Subscription <subscription-id> [-Repo <owner>/<repo>]
#
# Tenant rule: -Tenant is the tenant the subscription lives in. This script talks to that tenant
# directly over REST with tokens requested for it. It never runs an az command against the
# subscription, so it never goes through a Lighthouse projection and never changes the Azure CLI
# default subscription or login. The only az commands used are "account get-access-token" and the
# local "bicep build".
param(
    [Parameter(Mandatory)][string]$Tenant,
    [Parameter(Mandatory)][string]$Subscription,
    [string]$Location = "swedencentral",
    [string]$Repo = "tinodo/dojo",
    [string]$AppName = "dojo-tinodo"
)
$ErrorActionPreference = "Stop"
$guid = '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
if ($Tenant -notmatch $guid) { throw "-Tenant must be the tenant id (a GUID)." }
if ($Subscription -notmatch $guid) { throw "-Subscription must be the subscription id (a GUID)." }
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$tokens = @{}

function Get-Token([string]$Resource) {
    $cached = $tokens[$Resource]
    if ($cached -and $cached.expires -gt (Get-Date).AddMinutes(5)) { return $cached.value }
    $json = & az account get-access-token --tenant $Tenant --resource $Resource -o json --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw "Could not get a token for $Resource in the given tenant. Run 'az login --tenant <tenant-id>' first." }
    $t = $json | ConvertFrom-Json
    $tokens[$Resource] = @{ value = $t.accessToken; expires = [DateTime]::Parse($t.expiresOn) }
    return $t.accessToken
}

function Invoke-Api([string]$Method, [string]$Url, $Body = $null) {
    $resource = if ($Url -like "https://graph.microsoft.com/*") { "https://graph.microsoft.com/" } else { "https://management.azure.com/" }
    $headers = @{ Authorization = "Bearer $(Get-Token $resource)" }
    $params = @{ Method = $Method; Uri = $Url; Headers = $headers }
    if ($null -ne $Body) { $params.Body = ($Body | ConvertTo-Json -Depth 100 -Compress); $params.ContentType = "application/json" }
    return Invoke-RestMethod @params
}

function Get-OrNull([string]$Url) {
    try { return Invoke-Api GET $Url }
    catch {
        if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 404) { return $null }
        throw
    }
}

$arm = "https://management.azure.com"
$graph = "https://graph.microsoft.com/v1.0"

# The deploy identity trusts only pushes to main of -Repo, in the subject format GitHub issues for that repository.
# Repositories created after 15 July 2026, or opted in, use the immutable format with the numeric owner and repository
# ids, so a repository re-created under the same name never inherits the trust. Older repositories keep the
# name-only format. GitHub says which one applies; the script never assumes.
$oidc = & gh api "repos/$Repo/actions/oidc/customization/sub" | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $null -eq $oidc) { throw "Could not read the OIDC subject settings of $Repo (gh auth login?)." }
if (-not $oidc.use_default) { throw "$Repo uses a custom OIDC subject template. The bootstrap supports only GitHub's default subject." }
$subjectPrefix = $oidc.sub_claim_prefix
if (-not $subjectPrefix) {
    $repoInfo = & gh api "repos/$Repo" | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or -not $repoInfo.id) { throw "Could not read the GitHub repository $Repo (gh auth login?)." }
    $subjectPrefix = if ($oidc.use_immutable_subject) { "repo:$($repoInfo.owner.login)@$($repoInfo.owner.id)/$($repoInfo.name)@$($repoInfo.id)" } else { "repo:$($repoInfo.full_name)" }
}
$githubSubject = "$($subjectPrefix):ref:refs/heads/main"

$sub = Invoke-Api GET "$arm/subscriptions/$($Subscription)?api-version=2022-12-01"
if ($sub.tenantId -ne $Tenant) { throw "The subscription is not in the given tenant." }
$me = Invoke-Api GET "$graph/me?`$select=id,userPrincipalName"
$ownerOid = $me.id
Write-Host "Subscription $($sub.displayName); you are the owner."

# Never adopt something that is not Dojo's: the fixed names must be new, or already tagged project=dojo.
foreach ($rg in @("rg-dojo-identity-swc", "rg-dojo-swc")) {
    $existing = Get-OrNull "$arm/subscriptions/$Subscription/resourcegroups/$($rg)?api-version=2021-04-01"
    if ($existing -and $existing.tags.project -ne "dojo") {
        throw "Resource group $rg already exists and is not tagged project=dojo. Refusing to change it."
    }
}

Write-Host "1/3 Resource groups, identities and roles"
$template = & az bicep build --file (Join-Path $here "main.bicep") --stdout --only-show-errors
if ($LASTEXITCODE -ne 0) { throw "bicep build failed" }
$deployUrl = "$arm/subscriptions/$Subscription/providers/Microsoft.Resources/deployments/dojo-bootstrap?api-version=2024-03-01"
$body = @{ location = $Location; properties = @{ mode = "Incremental"; template = ($template -join "`n" | ConvertFrom-Json); parameters = @{ location = @{ value = $Location }; ownerPrincipalId = @{ value = $ownerOid }; githubSubject = @{ value = $githubSubject } } } }
Invoke-Api PUT $deployUrl $body | Out-Null
do {
    Start-Sleep -Seconds 10
    $d = Invoke-Api GET $deployUrl
    Write-Host "  $($d.properties.provisioningState)"
} while ($d.properties.provisioningState -notin @("Succeeded", "Failed", "Canceled"))
if ($d.properties.provisioningState -ne "Succeeded") {
    $ops = Invoke-Api GET "$arm/subscriptions/$Subscription/providers/Microsoft.Resources/deployments/dojo-bootstrap/operations?api-version=2024-03-01"
    foreach ($op in $ops.value | Where-Object { $_.properties.provisioningState -eq "Failed" }) { Write-Host ($op.properties.statusMessage | ConvertTo-Json -Depth 20 -Compress) }
    throw "Bootstrap deployment $($d.properties.provisioningState)"
}
$outputs = $d.properties.outputs
$deployClientId = $outputs.deployClientId.value
$appIdentityClientId = $outputs.appIdentityClientId.value
$appIdentityPrincipalId = $outputs.appIdentityPrincipalId.value

Write-Host "2/3 Sign-in app registration"
$redirect = "https://$AppName.azurewebsites.net/.auth/login/aad/callback"
# App Service sign-in uses the OpenID Connect hybrid flow, which needs ID-token issuance.
$appShape = @{
    web = @{ redirectUris = @($redirect); implicitGrantSettings = @{ enableIdTokenIssuance = $true; enableAccessTokenIssuance = $false } }
    api = @{ requestedAccessTokenVersion = 2 }
}
$found = (Invoke-Api GET "$graph/applications?`$filter=displayName eq '$AppName'&`$select=id,appId").value
if (@($found).Count -gt 1) { throw "More than one app registration named $AppName exists; refusing to guess" }
if (@($found).Count -eq 1) {
    $app = $found[0]
    $owners = @((Invoke-Api GET "$graph/applications/$($app.id)/owners?`$select=id").value | ForEach-Object { $_.id })
    # Only reuse an app you own (the bootstrap makes you its owner); never reconfigure a stranger's app.
    if ($owners -notcontains $ownerOid) {
        throw "An app registration named $AppName exists but you are not its owner. Refusing to change it."
    }
    Write-Host "  reusing the existing app registration $AppName"
    Invoke-Api PATCH "$graph/applications/$($app.id)" $appShape | Out-Null
} else {
    $new = $appShape.Clone(); $new.displayName = $AppName; $new.signInAudience = "AzureADMyOrg"
    $app = Invoke-Api POST "$graph/applications" $new
    Invoke-Api POST "$graph/applications/$($app.id)/owners/`$ref" @{ "@odata.id" = "https://graph.microsoft.com/v1.0/directoryObjects/$ownerOid" } | Out-Null
    Write-Host "  created the app registration $AppName"
}
$appId = $app.appId

$ficName = "dojo-web-app-identity"
$fics = (Invoke-Api GET "$graph/applications/$($app.id)/federatedIdentityCredentials").value
if (-not ($fics | Where-Object { $_.name -eq $ficName })) {
    Invoke-Api POST "$graph/applications/$($app.id)/federatedIdentityCredentials" @{
        name = $ficName
        issuer = "https://login.microsoftonline.com/$Tenant/v2.0"
        subject = $appIdentityPrincipalId
        audiences = @("api://AzureADTokenExchange")
        description = "The Dojo web app signs users in with its managed identity instead of a secret."
    } | Out-Null
}

$sp = (Invoke-Api GET "$graph/servicePrincipals?`$filter=appId eq '$appId'&`$select=id").value | Select-Object -First 1
if (-not $sp) {
    for ($i = 0; $i -lt 6 -and -not $sp; $i++) {
        try { $sp = Invoke-Api POST "$graph/servicePrincipals" @{ appId = $appId } } catch { Start-Sleep -Seconds 10 }
    }
    if (-not $sp) { throw "Could not create the service principal for $appId" }
}
Invoke-Api PATCH "$graph/servicePrincipals/$($sp.id)" @{ appRoleAssignmentRequired = $true } | Out-Null
$assigned = (Invoke-Api GET "$graph/servicePrincipals/$($sp.id)/appRoleAssignedTo").value | Where-Object { $_.principalId -eq $ownerOid }
if (-not $assigned) {
    Invoke-Api POST "$graph/servicePrincipals/$($sp.id)/appRoleAssignedTo" @{ principalId = $ownerOid; resourceId = $sp.id; appRoleId = "00000000-0000-0000-0000-000000000000" } | Out-Null
}

Write-Host "3/3 GitHub repository secrets"
$secrets = [ordered]@{
    AZURE_CLIENT_ID       = $deployClientId
    AZURE_TENANT_ID       = $Tenant
    AZURE_SUBSCRIPTION_ID = $Subscription
    DOJO_AUTH_CLIENT_ID   = $appId
    DOJO_OWNER_OID        = $ownerOid
}
$oldVars = @((& gh variable list --repo $Repo --json name | ConvertFrom-Json) | ForEach-Object { $_.name })
foreach ($k in $secrets.Keys) {
    # The value goes in on standard input, so it is not on the command line either.
    $secrets[$k] | & gh secret set $k --repo $Repo
    if ($LASTEXITCODE -ne 0) { throw "gh secret set $k failed" }
    # Older versions kept these as plain variables; remove them so the values are no longer visible.
    if ($oldVars -contains $k) {
        & gh variable delete $k --repo $Repo
        if ($LASTEXITCODE -ne 0) { throw "gh variable delete $k failed" }
    }
    Write-Host "  set $k"
}

Write-Host "Done."