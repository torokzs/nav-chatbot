targetScope = 'resourceGroup'

@description('Base name used for the Container Apps environment, workspace, and backend app.')
param name string

@description('Azure region for Container Apps resources.')
param location string = resourceGroup().location

@description('Tags applied to Container Apps resources.')
param tags object = {}

@description('Azure Container Registry login server used for image pulls.')
param containerRegistryLoginServer string

@description('Fully qualified backend container image reference.')
param backendContainerImage string

@description('Optional existing stable revision name. When supplied, the latest revision is deployed at 0% traffic for blue/green rollouts.')
param stableRevisionName string = ''

@description('Environment variables for the backend app (endpoints, config).')
param envVars object = {}

var workspaceName = take(replace('log-${name}', '--', '-'), 63)
var environmentName = take(replace('cae-${name}', '--', '-'), 60)
var backendAppName = take(replace('${name}-backend', '--', '-'), 32)
var trafficRules = empty(stableRevisionName) ? [
  {
    latestRevision: true
    weight: 100
  }
] : [
  {
    revisionName: stableRevisionName
    weight: 100
  }
  {
    latestRevision: true
    label: 'staging'
    weight: 0
  }
]

resource logAnalyticsWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: workspaceName
}

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: environmentName
}

resource backendApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: backendAppName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    managedEnvironmentId: containerAppsEnvironment.id
    configuration: {
      activeRevisionsMode: 'Multiple'
      ingress: {
        external: true
        targetPort: 80
        transport: 'http'
        allowInsecure: false
        traffic: trafficRules
      }
    }
    template: {
      containers: [
        {
          name: 'backend'
          image: backendContainerImage
          env: [
            {
              name: 'AZURE_AI_SEARCH_ENDPOINT'
              value: contains(envVars, 'aiSearchEndpoint') ? envVars.aiSearchEndpoint : ''
            }
            {
              name: 'AZURE_AI_FOUNDRY_ENDPOINT'
              value: contains(envVars, 'aiFoundryEndpoint') ? envVars.aiFoundryEndpoint : ''
            }
            {
              name: 'AZURE_AI_FOUNDRY_CHAT_DEPLOYMENT'
              value: 'model-router'
            }
            {
              name: 'AZURE_AI_FOUNDRY_EMBEDDING_DEPLOYMENT'
              value: 'text-embedding-3-large'
            }
            {
              name: 'AZURE_DOC_INTELLIGENCE_ENDPOINT'
              value: contains(envVars, 'docIntelligenceEndpoint') ? envVars.docIntelligenceEndpoint : ''
            }
            {
              name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
              value: contains(envVars, 'appInsightsConnectionString') ? envVars.appInsightsConnectionString : ''
            }
          ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 10
      }
    }
  }
}

@description('Backend Container App HTTPS URL.')
output fqdn string = 'https://${backendApp.properties.configuration.ingress.fqdn}'

@description('Backend Container App resource ID.')
output resourceId string = backendApp.id

@description('Backend Container App managed identity principal ID.')
output managedIdentityPrincipalId string = backendApp.identity.principalId
