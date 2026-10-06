// One-time bootstrap for Dojo, run once by the owner (see bootstrap.ps1).
// Creates only new Dojo resource groups and identities; it changes nothing that already exists.
targetScope = 'subscription'

@description('Azure region for all Dojo resources.')
param location string = 'swedencentral'

@description('Resource group that holds the identities. The deployment pipeline has no write access here.')
param identityResourceGroupName string = 'rg-dojo-identity-swc'

@description('Resource group that holds the application. The deployment pipeline is Contributor here only.')
param appResourceGroupName string = 'rg-dojo-swc'

@description('Exact GitHub OIDC subject allowed to deploy (main branch only). bootstrap.ps1 derives it from -Repo.')
param githubSubject string

@description('Owner user object ID; gets model access for local development.')
param ownerPrincipalId string = ''

param tags object = {
  project: 'dojo'
  owner: 'tinodo'
  purpose: 'personal-learning'
}

resource identityRg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: identityResourceGroupName
  location: location
  tags: tags
}

resource appRg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: appResourceGroupName
  location: location
  tags: tags
}

module identities 'identities.bicep' = {
  name: 'dojo-bootstrap-identities'
  scope: identityRg
  params: {
    location: location
    githubSubject: githubSubject
    tags: tags
  }
}

module appRoles 'app-roles.bicep' = {
  name: 'dojo-bootstrap-app-roles'
  scope: appRg
  params: {
    deployPrincipalId: identities.outputs.deployPrincipalId
    appPrincipalId: identities.outputs.appPrincipalId
    ownerPrincipalId: ownerPrincipalId
  }
}

output identityResourceGroup string = identityRg.name
output appResourceGroup string = appRg.name
output deployClientId string = identities.outputs.deployClientId
output appIdentityClientId string = identities.outputs.appClientId
output appIdentityPrincipalId string = identities.outputs.appPrincipalId
