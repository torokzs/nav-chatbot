targetScope = 'resourceGroup'

@description('Microsoft Fabric capacity name.')
param name string

@description('Azure region for the Microsoft Fabric capacity.')
param location string = resourceGroup().location

@description('Tags applied to the Fabric capacity.')
param tags object = {}

@description('Microsoft Fabric administrator UPNs. Deployment is skipped when empty.')
param adminMembers array = []

var deployCapacity = length(adminMembers) > 0

resource fabricCapacity 'Microsoft.Fabric/capacities@2023-11-01' = if (deployCapacity) {
  name: name
  location: location
  sku: {
    name: 'F4'
    tier: 'Fabric'
  }
  tags: tags
  properties: {
    administration: {
      members: adminMembers
    }
  }
}

@description('Microsoft Fabric capacity resource ID. Empty when deployment is skipped because no admins were supplied.')
output capacityId string = deployCapacity ? fabricCapacity.id : ''
