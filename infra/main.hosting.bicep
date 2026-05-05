targetScope = 'subscription'

@description('Azure region for hosting resources.')
param location string = 'swedencentral'

@description('Environment name for tagging.')
param environmentName string

@description('Resource name prefix.')
@minLength(3)
@maxLength(18)
param resourceNamePrefix string

@description('Backend container image. Use placeholder for initial deployment.')
param backendContainerImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@description('Static Web App location (must be westeurope for Europe).')
param swaLocation string = 'westeurope'

var uniqueSuffix = take(uniqueString(subscription().id, environmentName, location), 6)
var normalizedPrefix = toLower(resourceNamePrefix)
var resourceGroupName = 'nav-chatbot-rg'
var commonTags = {
  environment: environmentName
  project: 'nav-chatbot'
  'managed-by': 'bicep'
}

resource rg 'Microsoft.Resources/resourceGroups@2023-07-01' existing = {
  name: resourceGroupName
}

module containerRegistry './modules/container-registry.bicep' = {
  name: 'container-registry'
  scope: rg
  params: {
    name: 'acr${normalizedPrefix}${uniqueSuffix}'
    location: location
    tags: commonTags
  }
}

module containerApps './modules/container-apps.bicep' = {
  name: 'container-apps'
  scope: rg
  params: {
    name: 'ca-${normalizedPrefix}-${uniqueSuffix}'
    location: location
    tags: commonTags
    containerRegistryLoginServer: containerRegistry.outputs.loginServer
    backendContainerImage: backendContainerImage
    envVars: {
      aiSearchEndpoint: 'https://srch-${normalizedPrefix}-${uniqueSuffix}.search.windows.net'
      aiFoundryEndpoint: 'https://cog-${normalizedPrefix}-${uniqueSuffix}.cognitiveservices.azure.com'
      docIntelligenceEndpoint: 'https://di-${normalizedPrefix}-${uniqueSuffix}.cognitiveservices.azure.com'
    }
  }
}

module staticWebApp './modules/static-web-app.bicep' = {
  name: 'static-web-app'
  scope: rg
  params: {
    name: 'swa-${normalizedPrefix}-${uniqueSuffix}'
    location: swaLocation
    tags: commonTags
    backendApiUrl: containerApps.outputs.fqdn
  }
}

output CONTAINER_REGISTRY_LOGIN_SERVER string = containerRegistry.outputs.loginServer
output CONTAINER_APP_FQDN string = containerApps.outputs.fqdn
output STATIC_WEB_APP_URL string = staticWebApp.outputs.url
output CONTAINER_APP_MANAGED_IDENTITY_PRINCIPAL_ID string = containerApps.outputs.managedIdentityPrincipalId
