targetScope = 'resourceGroup'

@description('Azure Static Web App name.')
param name string

@description('Azure region for the Static Web App.')
param location string = resourceGroup().location

@description('Tags applied to the Static Web App.')
param tags object = {}

@description('Backend API URL for linked backend configuration.')
param backendApiUrl string = ''

resource staticWebApp 'Microsoft.Web/staticSites@2023-01-01' = {
  name: name
  location: location
  sku: {
    name: 'Standard'
    tier: 'Standard'
  }
  tags: tags
  properties: {
    stagingEnvironmentPolicy: 'Enabled'
    allowConfigFileUpdates: true
  }
}

@description('Static Web App URL.')
output url string = 'https://${staticWebApp.properties.defaultHostname}'

@description('Static Web App resource ID.')
output resourceId string = staticWebApp.id

@description('Static Web App name for CLI deployment.')
output name string = staticWebApp.name
