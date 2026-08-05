targetScope = 'resourceGroup'

@description('Container App managed identity principal ID.')
param containerAppPrincipalId string

@description('Optional GitHub Actions service principal object ID.')
param githubActionsPrincipalId string = ''

@description('Azure Key Vault resource ID.')
param keyVaultResourceId string

@description('Azure AI Search resource ID.')
param searchServiceResourceId string

@description('Azure AI Services resource ID.')
param aiFoundryResourceId string

@description('Azure AI Document Intelligence resource ID.')
param docIntelligenceResourceId string

@description('Azure Container Registry resource ID.')
param containerRegistryResourceId string

@description('Optional Storage Account resource ID for blob read access.')
param storageAccountResourceId string = ''

var contributorRoleDefinitionId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b24988ac-6180-42a0-ab88-20f7382dd24c')
var keyVaultSecretsUserRoleDefinitionId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
var keyVaultSecretsOfficerRoleDefinitionId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7')
var searchIndexDataReaderRoleDefinitionId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '1407120a-92aa-4202-b7e9-c0e197c71c8f')
var cognitiveServicesOpenAIUserRoleDefinitionId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
var cognitiveServicesUserRoleDefinitionId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'a97b65f3-24c7-4388-baec-2e87135dc908')
var storageBlobDataReaderRoleDefinitionId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1')
var acrPullRoleDefinitionId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
var assignGitHubActionsPrincipal = !empty(githubActionsPrincipalId)
var assignStorageRole = !empty(storageAccountResourceId)

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: last(split(keyVaultResourceId, '/'))
}

resource searchService 'Microsoft.Search/searchServices@2023-11-01' existing = {
  name: last(split(searchServiceResourceId, '/'))
}

resource aiFoundry 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: last(split(aiFoundryResourceId, '/'))
}

resource docIntelligence 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: last(split(docIntelligenceResourceId, '/'))
}

resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: last(split(containerRegistryResourceId, '/'))
}

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' existing = if (assignStorageRole) {
  name: last(split(storageAccountResourceId, '/'))
}

resource containerAppToKeyVaultSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, containerAppPrincipalId, keyVaultSecretsUserRoleDefinitionId)
  scope: keyVault
  properties: {
    roleDefinitionId: keyVaultSecretsUserRoleDefinitionId
    principalId: containerAppPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource containerAppToSearchIndexDataReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(searchService.id, containerAppPrincipalId, searchIndexDataReaderRoleDefinitionId)
  scope: searchService
  properties: {
    roleDefinitionId: searchIndexDataReaderRoleDefinitionId
    principalId: containerAppPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource containerAppToAiFoundryOpenAiUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(aiFoundry.id, containerAppPrincipalId, cognitiveServicesOpenAIUserRoleDefinitionId)
  scope: aiFoundry
  properties: {
    roleDefinitionId: cognitiveServicesOpenAIUserRoleDefinitionId
    principalId: containerAppPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource containerAppToDocIntelligenceUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(docIntelligence.id, containerAppPrincipalId, cognitiveServicesUserRoleDefinitionId)
  scope: docIntelligence
  properties: {
    roleDefinitionId: cognitiveServicesUserRoleDefinitionId
    principalId: containerAppPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource containerAppToAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(containerRegistry.id, containerAppPrincipalId, acrPullRoleDefinitionId)
  scope: containerRegistry
  properties: {
    roleDefinitionId: acrPullRoleDefinitionId
    principalId: containerAppPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource containerAppToStorageBlobDataReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (assignStorageRole) {
  name: guid(storageAccount.id, containerAppPrincipalId, storageBlobDataReaderRoleDefinitionId)
  scope: storageAccount
  properties: {
    roleDefinitionId: storageBlobDataReaderRoleDefinitionId
    principalId: containerAppPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource githubActionsToResourceGroupContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (assignGitHubActionsPrincipal) {
  name: guid(resourceGroup().id, githubActionsPrincipalId, 'Contributor')
  properties: {
    roleDefinitionId: contributorRoleDefinitionId
    principalId: githubActionsPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource githubActionsToKeyVaultSecretsOfficer 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (assignGitHubActionsPrincipal) {
  name: guid(keyVault.id, githubActionsPrincipalId, 'Key Vault Secrets Officer')
  scope: keyVault
  properties: {
    roleDefinitionId: keyVaultSecretsOfficerRoleDefinitionId
    principalId: githubActionsPrincipalId
    principalType: 'ServicePrincipal'
  }
}
