// Role assignments on the application resource group, made once by the owner so the
// pipeline itself never needs permission to assign roles.
param deployPrincipalId string
param appPrincipalId string

@description('Owner user object ID. Gets the same model and Speech access as the app so the local development harness can call the real models. Empty = none.')
param ownerPrincipalId string = ''

var contributor = 'b24988ac-6180-42a0-ab88-20f7382dd24c'

// Runtime identity: call Foundry models and Speech. Learner data lives on the web app's own
// persistent storage, so no data-store roles are needed.
var appRoles = [
  '53ca6127-db72-4b80-b1b0-d745d6d5456d' // Azure AI User (Foundry User)
  '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd' // Cognitive Services OpenAI User
  'f2dc8367-1007-4938-bd23-fe263f013447' // Cognitive Services Speech User
]

resource deployContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, deployPrincipalId, contributor)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', contributor)
    principalId: deployPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource runtimeRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for role in appRoles: {
  name: guid(resourceGroup().id, appPrincipalId, role)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', role)
    principalId: appPrincipalId
    principalType: 'ServicePrincipal'
  }
}]

// The owner's own account: model and Speech access for local development only (no data roles).
var ownerRoles = [
  '53ca6127-db72-4b80-b1b0-d745d6d5456d' // Azure AI User (Foundry User)
  '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd' // Cognitive Services OpenAI User
  'f2dc8367-1007-4938-bd23-fe263f013447' // Cognitive Services Speech User
]

resource ownerDevRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for role in (empty(ownerPrincipalId) ? [] : ownerRoles): {
  name: guid(resourceGroup().id, ownerPrincipalId, role)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', role)
    principalId: ownerPrincipalId
    principalType: 'User'
  }
}]