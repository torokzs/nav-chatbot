targetScope = 'resourceGroup'

@description('Name of existing Key Vault.')
param keyVaultName string

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

output secretUris object = {
  aiSearchEndpoint: '${keyVault.properties.vaultUri}secrets/ai-search-endpoint'
  aiFoundryEndpoint: '${keyVault.properties.vaultUri}secrets/ai-foundry-endpoint'
  docIntelligenceEndpoint: '${keyVault.properties.vaultUri}secrets/doc-intelligence-endpoint'
  appInsightsConnectionString: '${keyVault.properties.vaultUri}secrets/app-insights-connection-string'
}

output vaultUri string = keyVault.properties.vaultUri
