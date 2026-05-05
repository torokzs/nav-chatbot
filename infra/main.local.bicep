targetScope = 'subscription'

@description('Azure region for all resources.')
param location string = 'swedencentral'

@description('Environment name for tagging.')
param environmentName string

@description('Resource name prefix.')
@minLength(3)
@maxLength(18)
param resourceNamePrefix string

var uniqueSuffix = take(uniqueString(subscription().id, environmentName, location), 6)
var normalizedPrefix = toLower(resourceNamePrefix)
var resourceGroupName = 'nav-chatbot-rg'
var commonTags = {
  environment: environmentName
  project: 'nav-chatbot'
  'managed-by': 'bicep'
}

resource rg 'Microsoft.Resources/resourceGroups@2023-07-01' = {
  name: resourceGroupName
  location: location
  tags: commonTags
}

module appInsights './modules/app-insights.bicep' = {
  name: 'app-insights'
  scope: rg
  params: {
    name: 'appi-${normalizedPrefix}-${uniqueSuffix}'
    location: location
    tags: commonTags
  }
}

module aiSearch './modules/ai-search.bicep' = {
  name: 'ai-search'
  scope: rg
  params: {
    name: 'srch-${normalizedPrefix}-${uniqueSuffix}'
    location: location
    tags: commonTags
  }
}

module aiFoundry './modules/ai-foundry.bicep' = {
  name: 'ai-foundry'
  scope: rg
  params: {
    name: 'cog-${normalizedPrefix}-${uniqueSuffix}'
    location: location
    tags: commonTags
  }
}

module docIntelligence './modules/doc-intelligence.bicep' = {
  name: 'doc-intelligence'
  scope: rg
  params: {
    name: 'di-${normalizedPrefix}-${uniqueSuffix}'
    location: location
    tags: commonTags
  }
}

module keyVault './modules/key-vault.bicep' = {
  name: 'key-vault'
  scope: rg
  params: {
    name: take('kv-${normalizedPrefix}-${uniqueSuffix}', 24)
    location: location
    tags: commonTags
    aiSearchEndpoint: aiSearch.outputs.endpoint
    aiFoundryEndpoint: aiFoundry.outputs.endpoint
    docIntelligenceEndpoint: docIntelligence.outputs.endpoint
    appInsightsConnectionString: appInsights.outputs.connectionString
  }
}

output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_SEARCH_ENDPOINT string = aiSearch.outputs.endpoint
output AZURE_AI_FOUNDRY_ENDPOINT string = aiFoundry.outputs.endpoint
output AZURE_DOC_INTELLIGENCE_ENDPOINT string = docIntelligence.outputs.endpoint
output AZURE_KEY_VAULT_NAME string = keyVault.outputs.name
