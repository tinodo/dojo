// Dojo application infrastructure. Deployed ONLY by the GitHub Actions pipeline, in
// incremental mode, into rg-dojo-swc. It never touches any other resource group.
param location string = resourceGroup().location

@description('Base name; also the web app host name (<name>.azurewebsites.net).')
param name string = 'dojo-tinodo'

@description('Client (application) ID of the Entra app registration used for sign-in.')
param authClientId string

@description('Object ID of the owner. With teamMode off, the only person allowed to use this deployment.')
param ownerObjectId string

@description('Team Dojo (docs/adr/0009-team-dojo.md). Off: Easy Auth admits the owner only, exactly as before. On: Easy Auth admits every user of the tenant and the app admits the owner plus approved members. Switching it on is the owner\'s decision, after the outside privacy review.')
param teamMode bool = false

@description('Only for the owner\'s lost-account procedure (ADR 0009): the owner\'s old learner key, so a new owner object id reaches the same record. Empty: derived from the tenant and owner object id.')
param ownerLearnerKey string = ''

param identityResourceGroupName string = 'rg-dojo-identity-swc'
param appIdentityName string = 'id-dojo-app'

@description('Teaching model: writes lessons, practice, hints and narration.')
param authorModel object = {
  format: 'OpenAI'
  name: 'gpt-5.5'
  version: '2026-04-24'
  capacity: 150
}

@description('Checking model (a different family): gates everything shown to the learner and gives live feedback.')
param gateModel object = {
  format: 'xAI'
  name: 'grok-4-1-fast-reasoning'
  version: '1'
  capacity: 100
}

@description('Grading model (a third family): judges finished attempts and writes Findings.')
param graderModel object = {
  format: 'DeepSeek'
  name: 'DeepSeek-V4-Pro'
  version: '2026-04-23'
  capacity: 100
}

@description('Deployment type of the three models. The app looks up their list prices for this type (app/costs.py).')
param modelSku string = 'GlobalStandard'

param tags object = {
  project: 'dojo'
  owner: 'tinodo'
  purpose: 'personal-learning'
}

var tenantId = subscription().tenantId
var aiName = '${name}-ai'
var sqlName = '${name}-sql-${uniqueString(resourceGroup().id)}'
var sqlDatabaseName = 'sqldb-lab-${name}'

resource appIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: appIdentityName
  scope: resourceGroup(identityResourceGroupName)
}

// ---------- Azure AI Foundry ----------
resource ai 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: aiName
  location: location
  tags: tags
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    customSubDomainName: aiName
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
    allowProjectManagement: true
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: ai
  name: 'dojo'
  location: location
  tags: tags
  // The account accepts one change at a time; the project waits for the last model deployment.
  dependsOn: [
    graderDeployment
  ]
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: 'Dojo'
    description: 'Personal AI trainer for Microsoft and GitHub certifications.'
  }
}

resource authorDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: ai
  name: 'author'
  sku: {
    name: modelSku
    capacity: authorModel.capacity
  }
  properties: {
    model: {
      format: authorModel.format
      name: authorModel.name
      version: authorModel.version
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

resource gateDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: ai
  name: 'gate'
  dependsOn: [
    authorDeployment
  ]
  sku: {
    name: modelSku
    capacity: gateModel.capacity
  }
  properties: {
    model: {
      format: gateModel.format
      name: gateModel.name
      version: gateModel.version
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

resource graderDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: ai
  name: 'grader'
  dependsOn: [
    gateDeployment
  ]
  sku: {
    name: modelSku
    capacity: graderModel.capacity
  }
  properties: {
    model: {
      format: graderModel.format
      name: graderModel.name
      version: graderModel.version
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

// ---------- Learner records, generated content and narration ----------
// Stored on the web app's persistent /home file share (DOJO_DATA_DIR). The tenant's policies switch
// public network access off on new storage and database accounts, and a single learner does not need
// a separate database, so there is no Cosmos DB or storage account to protect or reach privately.
// ---------- Web app ----------
resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: 'plan-${name}'
  location: location
  tags: tags
  kind: 'linux'
  sku: {
    name: 'B1'
  }
  properties: {
    reserved: true
  }
}

// Only traffic for private addresses enters this VNet. AI Services and source pages use the
// site's ordinary public egress, as before.
resource labVnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: 'vnet-${name}-lab'
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: ['10.72.0.0/16']
    }
  }
}

resource integrationSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' = {
  parent: labVnet
  name: 'app'
  properties: {
    addressPrefix: '10.72.1.0/26'
    delegations: [
      {
        name: 'web'
        properties: {
          serviceName: 'Microsoft.Web/serverFarms'
        }
      }
    ]
  }
}

resource endpointSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' = {
  parent: labVnet
  name: 'endpoints'
  properties: {
    addressPrefix: '10.72.2.0/27'
    privateEndpointNetworkPolicies: 'Disabled'
  }
}

resource sqlServer 'Microsoft.Sql/servers@2023-08-01' = {
  name: sqlName
  location: location
  tags: tags
  properties: {
    version: '12.0'
    minimalTlsVersion: '1.2'
    publicNetworkAccess: 'Disabled'
    administrators: {
      administratorType: 'ActiveDirectory'
      login: appIdentity.name
      sid: appIdentity.properties.clientId
      tenantId: tenantId
      principalType: 'Application'
      azureADOnlyAuthentication: true
    }
  }
}

resource labDatabase 'Microsoft.Sql/servers/databases@2023-08-01' = {
  parent: sqlServer
  name: sqlDatabaseName
  location: location
  tags: tags
  sku: {
    name: 'GP_S_Gen5_2'
    tier: 'GeneralPurpose'
    family: 'Gen5'
    capacity: 2
  }
  properties: {
    useFreeLimit: true
    freeLimitExhaustionBehavior: 'AutoPause'
    autoPauseDelay: 60
    minCapacity: json('0.5')
    maxSizeBytes: 34359738368
    requestedBackupStorageRedundancy: 'Local'
  }
}

resource sqlDns 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink${environment().suffixes.sqlServerHostname}'
  location: 'global'
  tags: tags
}

resource sqlDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: sqlDns
  name: 'link-${name}-lab'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: labVnet.id
    }
  }
}

resource sqlEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'pe-${name}-sql'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: endpointSubnet.id
    }
    privateLinkServiceConnections: [
      {
        name: 'sql'
        properties: {
          privateLinkServiceId: sqlServer.id
          groupIds: ['sqlServer']
        }
      }
    ]
  }
}

resource sqlDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: sqlEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'sql'
        properties: {
          privateDnsZoneId: sqlDns.id
        }
      }
    ]
  }
}

resource site 'Microsoft.Web/sites@2024-04-01' = {
  name: name
  location: location
  tags: tags
  kind: 'app,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${appIdentity.id}': {}
    }
  }
  properties: {
    serverFarmId: plan.id
    virtualNetworkSubnetId: integrationSubnet.id
    vnetRouteAllEnabled: false
    httpsOnly: true
    clientAffinityEnabled: false
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.12'
      alwaysOn: true
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      http20Enabled: true
      healthCheckPath: '/healthz'
      appCommandLine: 'gunicorn --bind=0.0.0.0:8000 --workers 1 --threads 1 --worker-class uvicorn.workers.UvicornWorker --timeout 600 app.main:app'
      appSettings: concat([
        {
          name: 'SCM_DO_BUILD_DURING_DEPLOYMENT'
          value: 'true'
        }
        {
          name: 'AZURE_CLIENT_ID'
          value: appIdentity.properties.clientId
        }
        {
          name: 'OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID'
          value: appIdentity.properties.clientId
        }
        {
          name: 'DOJO_AI_ENDPOINT'
          value: 'https://${aiName}.services.ai.azure.com/openai/v1'
        }
        {
          name: 'DOJO_AI_RESOURCE_ID'
          value: ai.id
        }
        {
          name: 'DOJO_SPEECH_ENDPOINT'
          value: 'https://${aiName}.cognitiveservices.azure.com'
        }
        {
          name: 'DOJO_MODELS'
          value: '{"author":"${authorModel.name}@${authorModel.version}","gate":"${gateModel.name}@${gateModel.version}","grader":"${graderModel.name}@${graderModel.version}"}'
        }
        {
          name: 'DOJO_MODEL_SKU'
          value: modelSku
        }
        {
          name: 'DOJO_DATA_DIR'
          value: '/home/dojo-data'
        }
        {
          name: 'DOJO_LAB_SERVER'
          value: '${sqlName}${environment().suffixes.sqlServerHostname}'
        }
        {
          name: 'DOJO_LAB_DATABASE'
          value: labDatabase.name
        }
        {
          name: 'WEBSITES_ENABLE_APP_SERVICE_STORAGE'
          value: 'true'
        }
        {
          name: 'DOJO_SPEECH_REGION'
          value: location
        }
        {
          name: 'DOJO_OWNER_TID'
          value: tenantId
        }
        {
          name: 'DOJO_OWNER_OID'
          value: ownerObjectId
        }
        {
          name: 'DOJO_AUTH_CLIENT_ID'
          value: authClientId
        }
        {
          name: 'DOJO_TEAM'
          value: teamMode ? '1' : '0'
        }
      ], empty(ownerLearnerKey) ? [] : [
        {
          name: 'DOJO_OWNER_KEY'
          value: ownerLearnerKey
        }
      ], teamMode ? [
        // Easy Auth also checks the token's tid claim against this list (ADR 0009). The app checks it too.
        {
          name: 'WEBSITE_AUTH_AAD_ALLOWED_TENANTS'
          value: tenantId
        }
      ] : [])
    }
  }
}

resource siteLogs 'Microsoft.Web/sites/config@2024-04-01' = {
  parent: site
  name: 'logs'
  properties: {
    // The App Service authentication module logs every request URL at Information, into
    // /home/LogFiles/Application/diagnostics-*.txt. The feed's URLs carry its secret, so it logs from
    // Warning up: its warnings and errors stay, request URLs do not (docs/adr/0002-private-podcast-feed.md).
    applicationLogs: {
      fileSystem: {
        level: 'Warning'
      }
    }
    // On Linux this switch keeps the container's console output (Dojo's own log, gunicorn's error log) for
    // 7 days; web server (W3C request) logs are Windows only. None of that output holds a feed URL: Dojo
    // logs /api paths only and gunicorn has no access log.
    httpLogs: {
      fileSystem: {
        enabled: true
        retentionInDays: 7
        retentionInMb: 35
      }
    }
    detailedErrorMessages: {
      enabled: false
    }
    failedRequestsTracing: {
      enabled: false
    }
  }
}

resource scmPublishing 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-04-01' = {
  parent: site
  name: 'scm'
  properties: {
    allow: false
  }
}

resource ftpPublishing 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-04-01' = {
  parent: site
  name: 'ftp'
  properties: {
    allow: false
  }
}

// Sign-in: Entra ID. Owner only unless teamMode is on; then every user of the tenant passes Easy Auth and
// the app's own allow-list (the owner plus approved members) is the gate (ADR 0009). No client secret: the
// app's managed identity is the credential.
resource auth 'Microsoft.Web/sites/config@2024-04-01' = {
  parent: site
  name: 'authsettingsV2'
  properties: {
    platform: {
      enabled: true
    }
    globalValidation: {
      requireAuthentication: true
      unauthenticatedClientAction: 'RedirectToLoginPage'
      redirectToProvider: 'azureactivedirectory'
      // Podcast and calendar apps cannot sign in, so /feed/* and /cal/* are open; the random token in the
      // path is the credential (docs/adr/0002-private-podcast-feed.md, docs/adr/0004-private-calendar-feed.md).
      // The app refuses dot segments and encoded separators.
      // The browser fetches two files outside any page: the service worker's script, again at every page load
      // in its scope (the whole site), and a nudge's icon. Sent to sign-in, each answer would set a new nonce
      // cookie, which replaces the nonce of a sign-in under way: that sign-in then fails ("invalid nonce") and
      // starts again, endlessly. Both are static, public files (docs/adr/0013-phone.md).
      excludedPaths: [
        '/healthz'
        '/feed/*'
        '/cal/*'
        '/static/sw.js'
        '/static/icon-192.png'
      ]
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: authClientId
          clientSecretSettingName: 'OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID'
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenantId}/v2.0'
        }
        // No allowedPrincipals.groups and no jwtClaimChecks: their behaviour for guests is undocumented.
        validation: union({
          allowedAudiences: [
            authClientId
            'api://${authClientId}'
          ]
        }, teamMode ? {} : {
          defaultAuthorizationPolicy: {
            allowedPrincipals: {
              identities: [
                ownerObjectId
              ]
            }
          }
        })
      }
    }
    login: {
      tokenStore: {
        enabled: true
      }
      // Left on, but nothing relies on it: seen on 5 Oct 2026, the fragment is still lost at sign-in (Easy Auth's
      // cookie for it is not sent on the cross-site return from Entra). Links that Dojo hands out use /?go=<route>.
      preserveUrlFragmentsForLogins: true
    }
  }
}

output siteName string = site.name
output siteUrl string = 'https://${site.properties.defaultHostName}'
output aiEndpoint string = 'https://${aiName}.services.ai.azure.com'
output projectName string = project.name
