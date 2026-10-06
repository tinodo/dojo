// Identities for Dojo. They live in their own resource group so the deployment
// pipeline can never change them or add credentials to itself (guardrail EG-7).
param location string
param githubSubject string
param tags object

var managedIdentityOperator = 'f1a07417-d97a-45cb-824c-7a7467783830'

resource deployIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-dojo-deploy'
  location: location
  tags: tags
}

resource githubMain 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: deployIdentity
  name: 'github-main'
  properties: {
    issuer: 'https://token.actions.githubusercontent.com'
    subject: githubSubject
    audiences: [
      'api://AzureADTokenExchange'
    ]
  }
}

resource appIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-dojo-app'
  location: location
  tags: tags
}

// The pipeline may attach the runtime identity to the web app, nothing more.
resource assignAppIdentity 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(appIdentity.id, deployIdentity.id, managedIdentityOperator)
  scope: appIdentity
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', managedIdentityOperator)
    principalId: deployIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

output deployClientId string = deployIdentity.properties.clientId
output deployPrincipalId string = deployIdentity.properties.principalId
output appClientId string = appIdentity.properties.clientId
output appPrincipalId string = appIdentity.properties.principalId
