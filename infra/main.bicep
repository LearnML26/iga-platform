// ============================================================================
// IGA Platform — Main Infrastructure Orchestration
// Implements: REQ-INF-001..005 (resource organization), orchestrates all modules
// Deploy:  az deployment sub create --location <region> \
//            --template-file main.bicep --parameters dev.bicepparam
// ============================================================================
targetScope = 'subscription'

@allowed(['dev', 'test', 'uat', 'prod'])
param environment string

@description('Primary Azure region')
param location string = 'eastus'

@description('Short app identifier used in resource names')
param appName string = 'iga'

@description('Object ID of the platform admin group granted Key Vault / AKS admin')
param adminGroupObjectId string

@description('Tags applied to every resource (REQ-INF-002/005)')
param tags object = {
  Application: 'iga-platform'
  Environment: environment
  Owner: 'iam-program'
  CostCenter: 'security'
  DataClassification: 'confidential'
}

var isProd = environment == 'prod'
var suffix = '${appName}-${environment}'

// ---------------------------------------------------------------------------
// Resource groups (REQ-INF-001)
// ---------------------------------------------------------------------------
resource rgNetwork 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${suffix}-network'
  location: location
  tags: tags
}
resource rgData 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${suffix}-data'
  location: location
  tags: tags
}
resource rgCompute 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${suffix}-compute'
  location: location
  tags: tags
}
resource rgSecurity 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${suffix}-security'
  location: location
  tags: tags
}

// ---------------------------------------------------------------------------
// Modules
// ---------------------------------------------------------------------------
module network 'modules/network.bicep' = {
  scope: rgNetwork
  name: 'network'
  params: {
    location: location
    suffix: suffix
    tags: tags
  }
}

module observability 'modules/observability.bicep' = {
  scope: rgSecurity
  name: 'observability'
  params: {
    location: location
    suffix: suffix
    tags: tags
    isProd: isProd
  }
}

module security 'modules/security.bicep' = {
  scope: rgSecurity
  name: 'security'
  params: {
    location: location
    suffix: suffix
    tags: tags
    adminGroupObjectId: adminGroupObjectId
    dataSubnetId: network.outputs.dataSubnetId
  }
}

module data 'modules/data.bicep' = {
  scope: rgData
  name: 'data'
  params: {
    location: location
    suffix: suffix
    tags: tags
    isProd: isProd
    dataSubnetId: network.outputs.dataSubnetId
    logAnalyticsId: observability.outputs.logAnalyticsId
    adminGroupObjectId: adminGroupObjectId
  }
}

module messaging 'modules/messaging.bicep' = {
  scope: rgData
  name: 'messaging'
  params: {
    location: location
    suffix: suffix
    tags: tags
    isProd: isProd
    dataSubnetId: network.outputs.dataSubnetId
  }
}

module compute 'modules/compute.bicep' = {
  scope: rgCompute
  name: 'compute'
  params: {
    location: location
    suffix: suffix
    tags: tags
    isProd: isProd
    aksSubnetId: network.outputs.aksSubnetId
    adminGroupObjectId: adminGroupObjectId
    logAnalyticsId: observability.outputs.logAnalyticsId
  }
}

// ---------------------------------------------------------------------------
// Outputs consumed by deploy.sh and CI
// ---------------------------------------------------------------------------
output aksName string = compute.outputs.aksName
output acrLoginServer string = compute.outputs.acrLoginServer
output keyVaultName string = security.outputs.keyVaultName
output cosmosAccountName string = data.outputs.cosmosAccountName
output sqlServerName string = data.outputs.sqlServerName
output storageAccountName string = data.outputs.storageAccountName
output serviceBusNamespace string = messaging.outputs.serviceBusNamespace
output eventHubNamespace string = messaging.outputs.eventHubNamespace
output computeResourceGroup string = rgCompute.name
output dataResourceGroup string = rgData.name
