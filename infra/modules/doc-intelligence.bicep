targetScope = 'resourceGroup'

@description('Azure AI Document Intelligence account name.')
param name string

@description('Azure region for Azure AI Document Intelligence.')
param location string = resourceGroup().location

@description('Tags applied to the Document Intelligence account.')
param tags object = {}

resource docIntelligence 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: name
  location: location
  kind: 'FormRecognizer'
  sku: {
    name: 'S0'
  }
  tags: tags
  properties: {
    customSubDomainName: name
    disableLocalAuth: false
    dynamicThrottlingEnabled: true
    publicNetworkAccess: 'Enabled'
  }
}

@description('Azure AI Document Intelligence endpoint.')
output endpoint string = docIntelligence.properties.endpoint

@description('Azure AI Document Intelligence resource ID.')
output resourceId string = docIntelligence.id
