targetScope = 'resourceGroup'

@description('Azure AI Services account name used for Azure AI Foundry-connected workloads.')
param name string

@description('Azure region for Azure AI Services.')
param location string = resourceGroup().location

@description('Tags applied to the Azure AI Services account.')
param tags object = {}

resource aiServices 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: name
  location: location
  kind: 'AIServices'
  identity: {
    type: 'SystemAssigned'
  }
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

// Model deployments such as model-router and text-embedding-3-large are typically created
// after the base account exists because availability is region and quota dependent.
@description('Azure AI Services endpoint.')
output endpoint string = aiServices.properties.endpoint

@description('Azure AI Services resource ID.')
output resourceId string = aiServices.id
