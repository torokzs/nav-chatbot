targetScope = 'resourceGroup'

@description('Azure Key Vault name.')
param name string

@description('Azure region for Azure Key Vault.')
param location string = resourceGroup().location

@description('Tags applied to Azure Key Vault and its secrets.')
param tags object = {}

@description('Azure AI Search endpoint value stored as a Key Vault secret.')
param aiSearchEndpoint string

@description('Azure AI Services endpoint value stored as a Key Vault secret.')
param aiFoundryEndpoint string

@description('Azure AI Document Intelligence endpoint value stored as a Key Vault secret.')
param docIntelligenceEndpoint string

@description('Application Insights connection string stored as a Key Vault secret.')
@secure()
param appInsightsConnectionString string

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enablePurgeProtection: true
    enableSoftDelete: true
    enabledForDeployment: false
    enabledForDiskEncryption: false
    enabledForTemplateDeployment: false
    publicNetworkAccess: 'Enabled'
    sku: {
      family: 'A'
      name: 'standard'
    }
    softDeleteRetentionInDays: 90
  }
}

resource aiSearchEndpointSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'ai-search-endpoint'
  properties: {
    value: aiSearchEndpoint
  }
}

resource aiFoundryEndpointSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'ai-foundry-endpoint'
  properties: {
    value: aiFoundryEndpoint
  }
}

resource docIntelligenceEndpointSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'doc-intelligence-endpoint'
  properties: {
    value: docIntelligenceEndpoint
  }
}

resource appInsightsConnectionStringSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'app-insights-connection-string'
  properties: {
    value: appInsightsConnectionString
  }
}

@description('Azure Key Vault URI.')
output vaultUri string = keyVault.properties.vaultUri

@description('Azure Key Vault resource ID.')
output resourceId string = keyVault.id

@description('Azure Key Vault name.')
output name string = keyVault.name

@description('AI Search endpoint secret URI with version.')
output aiSearchEndpointSecretUri string = aiSearchEndpointSecret.properties.secretUriWithVersion

@description('AI Foundry endpoint secret URI with version.')
output aiFoundryEndpointSecretUri string = aiFoundryEndpointSecret.properties.secretUriWithVersion

@description('Document Intelligence endpoint secret URI with version.')
output docIntelligenceEndpointSecretUri string = docIntelligenceEndpointSecret.properties.secretUriWithVersion

@description('Application Insights connection string secret URI with version.')
output appInsightsConnectionStringSecretUri string = appInsightsConnectionStringSecret.properties.secretUriWithVersion
