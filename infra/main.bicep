targetScope = 'subscription'

@description('Azure region for all regional resources.')
param location string = 'swedencentral'

@description('AZD environment name used for tagging and deterministic naming.')
@minLength(1)
@maxLength(64)
param environmentName string

@description('Project-wide resource name prefix used to derive Azure resource names.')
@minLength(3)
@maxLength(18)
param resourceNamePrefix string

@description('Optional GitHub Actions service principal object ID for deployment RBAC.')
param githubActionsPrincipalId string = ''

@description('Optional list of Microsoft Fabric capacity admin UPNs. Fabric deployment is skipped when empty.')
param fabricAdminMembers array = []

@description('Backend container image repository name inside Azure Container Registry.')
param backendImageName string = 'backend'

@description('Backend container image tag.')
param backendImageTag string = 'latest'

@description('Backend container image used during initial provisioning before azd builds the application image.')
param backendBootstrapImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@description('Optional existing stable Container App revision name. When supplied, the latest revision is deployed with 0% traffic for blue/green promotion workflows.')
param backendStableRevisionName string = ''

@description('Deploy hosting resources (Container Apps, Static Web App, Container Registry). Set false for local dev with only AI services.')
param deployHosting bool = true

var abbreviations = loadJsonContent('./abbreviations.json')
var uniqueSuffix = take(uniqueString(subscription().id, environmentName, location), 6)
var normalizedPrefix = toLower(resourceNamePrefix)
var resourceGroupName = 'nav-chatbot-rg'
var commonTags = {
  environment: environmentName
  project: 'nav-chatbot'
  'managed-by': 'bicep'
  'azd-env-name': environmentName
}
var searchServiceName = take('${abbreviations.aiSearch}-${normalizedPrefix}-${uniqueSuffix}', 60)
var aiFoundryName = take('${abbreviations.aiFoundry}-${normalizedPrefix}-${uniqueSuffix}', 64)
var docIntelligenceName = take('${abbreviations.documentIntelligence}-${normalizedPrefix}-${uniqueSuffix}', 64)
var keyVaultName = take('${abbreviations.keyVault}-${normalizedPrefix}-${uniqueSuffix}', 24)
var appInsightsName = take('${abbreviations.applicationInsights}-${normalizedPrefix}-${uniqueSuffix}', 64)
var containerRegistryName = take(replace('${abbreviations.containerRegistry}${normalizedPrefix}${uniqueSuffix}', '-', ''), 50)
var containerAppsName = take('${abbreviations.containerApp}-${normalizedPrefix}-${uniqueSuffix}', 32)
var staticWebAppName = take('${abbreviations.staticWebApp}-${normalizedPrefix}-${uniqueSuffix}', 40)
var fabricCapacityName = take('${abbreviations.fabricCapacity}-${normalizedPrefix}-${uniqueSuffix}', 63)

resource resourceGroup 'Microsoft.Resources/resourceGroups@2023-07-01' = {
  name: resourceGroupName
  location: location
  tags: commonTags
}

module appInsights './modules/app-insights.bicep' = {
  name: 'app-insights'
  scope: resourceGroup
  params: {
    name: appInsightsName
    location: location
    tags: commonTags
  }
}

module aiSearch './modules/ai-search.bicep' = {
  name: 'ai-search'
  scope: resourceGroup
  params: {
    name: searchServiceName
    location: location
    tags: commonTags
  }
}

module aiFoundry './modules/ai-foundry.bicep' = {
  name: 'ai-foundry'
  scope: resourceGroup
  params: {
    name: aiFoundryName
    location: location
    tags: commonTags
  }
}

module docIntelligence './modules/doc-intelligence.bicep' = {
  name: 'doc-intelligence'
  scope: resourceGroup
  params: {
    name: docIntelligenceName
    location: location
    tags: commonTags
  }
}

module containerRegistry './modules/container-registry.bicep' = if (deployHosting) {
  name: 'container-registry'
  scope: resourceGroup
  params: {
    name: containerRegistryName
    location: location
    tags: commonTags
  }
}

module keyVault './modules/key-vault.bicep' = {
  name: 'key-vault'
  scope: resourceGroup
  params: {
    name: keyVaultName
    location: location
    tags: commonTags
    aiSearchEndpoint: aiSearch.outputs.endpoint
    aiFoundryEndpoint: aiFoundry.outputs.endpoint
    docIntelligenceEndpoint: docIntelligence.outputs.endpoint
    appInsightsConnectionString: appInsights.outputs.connectionString
    frontendOrigin: deployHosting ? staticWebApp.outputs.url : ''
  }
}

module containerApps './modules/container-apps.bicep' = if (deployHosting) {
  name: 'container-apps'
  scope: resourceGroup
  params: {
    name: containerAppsName
    location: location
    tags: union(commonTags, {
      'azd-service-name': 'backend'
    })
    containerRegistryLoginServer: deployHosting ? containerRegistry.outputs.loginServer : ''
    backendContainerImage: backendBootstrapImage
    stableRevisionName: backendStableRevisionName
    envVars: {
      aiSearchEndpoint: aiSearch.outputs.endpoint
      aiFoundryEndpoint: aiFoundry.outputs.endpoint
      docIntelligenceEndpoint: docIntelligence.outputs.endpoint
      appInsightsConnectionString: appInsights.outputs.connectionString
    }
  }
}

module staticWebApp './modules/static-web-app.bicep' = if (deployHosting) {
  name: 'static-web-app'
  scope: resourceGroup
  params: {
    name: staticWebAppName
    location: 'westeurope' // SWA not available in swedencentral
    tags: union(commonTags, {
      'azd-service-name': 'frontend'
    })
  }
}

module fabric './modules/fabric.bicep' = {
  name: 'fabric-capacity'
  scope: resourceGroup
  params: {
    name: fabricCapacityName
    location: location
    tags: commonTags
    adminMembers: fabricAdminMembers
  }
}

module rbac './modules/rbac.bicep' = if (deployHosting) {
  name: 'rbac'
  scope: resourceGroup
  params: {
    containerAppPrincipalId: deployHosting ? containerApps.outputs.managedIdentityPrincipalId : ''
    keyVaultResourceId: keyVault.outputs.resourceId
    searchServiceResourceId: aiSearch.outputs.resourceId
    aiFoundryResourceId: aiFoundry.outputs.resourceId
    docIntelligenceResourceId: docIntelligence.outputs.resourceId
    containerRegistryResourceId: deployHosting ? containerRegistry.outputs.resourceId : ''
  }
}

@description('Provisioned Azure resource group name.')
output AZURE_RESOURCE_GROUP string = resourceGroup.name

@description('Backend Container App URL.')
output backendUrl string = deployHosting ? containerApps.outputs.fqdn : ''

@description('Frontend Static Web App URL.')
output frontendUrl string = deployHosting ? staticWebApp.outputs.url : ''

@description('Azure AI Search endpoint.')
output searchEndpoint string = aiSearch.outputs.endpoint

@description('Azure Key Vault name.')
output keyVaultName string = keyVault.outputs.name

@description('Backend Container App URL for azd environment projection.')
output API_URL string = deployHosting ? containerApps.outputs.fqdn : ''

@description('Frontend Static Web App URL for azd environment projection.')
output WEB_URL string = deployHosting ? staticWebApp.outputs.url : ''

@description('Azure AI Search endpoint for azd environment projection.')
output AZURE_SEARCH_ENDPOINT string = aiSearch.outputs.endpoint

@description('Azure Key Vault name for azd environment projection.')
output AZURE_KEY_VAULT_NAME string = keyVault.outputs.name

@description('Azure AI Foundry endpoint.')
output AZURE_AI_FOUNDRY_ENDPOINT string = aiFoundry.outputs.endpoint

@description('Azure Document Intelligence endpoint.')
output AZURE_DOC_INTELLIGENCE_ENDPOINT string = docIntelligence.outputs.endpoint

@description('Azure Container Registry login server for azd environment projection.')
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = containerRegistry.outputs.loginServer
