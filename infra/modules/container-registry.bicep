targetScope = 'resourceGroup'

@description('Azure Container Registry name.')
param name string

@description('Azure region for Azure Container Registry.')
param location string = resourceGroup().location

@description('Tags applied to Azure Container Registry.')
param tags object = {}

resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: name
  location: location
  sku: {
    name: 'Standard'
  }
  tags: tags
  properties: {
    adminUserEnabled: false
    anonymousPullEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

@description('Azure Container Registry login server.')
output loginServer string = containerRegistry.properties.loginServer

@description('Azure Container Registry resource ID.')
output resourceId string = containerRegistry.id
