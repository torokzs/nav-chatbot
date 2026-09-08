targetScope = 'resourceGroup'

@description('Azure AI Search service name.')
param name string

@description('Azure region for Azure AI Search.')
param location string = resourceGroup().location

@description('Tags applied to the Azure AI Search service.')
param tags object = {}

@description('Azure AI Search dedicated pricing tier.')
@allowed([
  'basic'
  'standard'
])
param skuName string = 'basic'

resource searchService 'Microsoft.Search/searchServices@2023-11-01' = {
  name: name
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  sku: {
    name: skuName
  }
  tags: tags
  properties: {
    authOptions: {
      aadOrApiKey: {
        aadAuthFailureMode: 'http401WithBearerChallenge'
      }
    }
    disableLocalAuth: false
    hostingMode: 'default'
    partitionCount: 1
    publicNetworkAccess: 'enabled'
    replicaCount: 1
    semanticSearch: 'standard'
  }
}

@description('Azure AI Search endpoint.')
output endpoint string = 'https://${searchService.name}.search.windows.net'

@description('Azure AI Search resource ID.')
output resourceId string = searchService.id

@description('Azure AI Search service name.')
output name string = searchService.name
