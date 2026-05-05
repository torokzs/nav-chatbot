using '../main.bicep'

param location = environment().name == 'AzureCloud' ? 'swedencentral' : 'westeurope'
param environmentName = readEnvironmentVariable('AZURE_ENV_NAME', 'dev')
param resourceNamePrefix = readEnvironmentVariable('RESOURCE_NAME_PREFIX', 'navchatbot')
param githubActionsPrincipalId = readEnvironmentVariable('GITHUB_ACTIONS_PRINCIPAL_ID', '')
param backendImageTag = readEnvironmentVariable('BACKEND_IMAGE_TAG', 'latest')
param backendStableRevisionName = readEnvironmentVariable('BACKEND_STABLE_REVISION_NAME', '')
