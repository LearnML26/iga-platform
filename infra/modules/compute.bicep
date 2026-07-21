// ============================================================================
// Compute module — REQ-INF-030..036
// AKS (workload identity, autoscaler, AZs in prod) + ACR
// ============================================================================
param location string
param suffix string
param tags object
param isProd bool
param aksSubnetId string
param adminGroupObjectId string
param logAnalyticsId string

var cleanSuffix = replace(suffix, '-', '')

resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: take('acr${cleanSuffix}', 50)
  location: location
  tags: tags
  sku: { name: isProd ? 'Premium' : 'Standard' }   // Premium = geo-replication + PE (REQ-INF-032)
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: isProd ? 'Disabled' : 'Enabled'
  }
}

resource aks 'Microsoft.ContainerService/managedClusters@2024-05-01' = {
  name: 'aks-${suffix}'
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    dnsPrefix: 'aks-${suffix}'
    enableRBAC: true
    disableLocalAccounts: true
    aadProfile: {
      managed: true
      enableAzureRBAC: true
      adminGroupObjectIDs: [adminGroupObjectId]
    }
    oidcIssuerProfile: { enabled: true }                    // Workload identity (REQ-INF-031)
    securityProfile: {
      workloadIdentity: { enabled: true }
    }
    networkProfile: {
      networkPlugin: 'azure'                                // Azure CNI (REQ-INF-030)
      networkPolicy: 'azure'                                // NetworkPolicy support (REQ-INF-033)
      serviceCidr: '10.100.0.0/16'
      dnsServiceIP: '10.100.0.10'
    }
    agentPoolProfiles: [
      {
        name: 'system'
        mode: 'System'
        vmSize: isProd ? 'Standard_D4s_v6' : 'Standard_D2s_v6'
        count: isProd ? 3 : 1
        minCount: isProd ? 3 : 1
        maxCount: isProd ? 6 : 2
        enableAutoScaling: true
        availabilityZones: isProd ? ['1', '2'] : null
        vnetSubnetID: aksSubnetId
        osType: 'Linux'
      }
      {
        name: 'user'
        mode: 'User'
        vmSize: isProd ? 'Standard_D4s_v6' : 'Standard_D2s_v6'
        count: isProd ? 3 : 1
        minCount: isProd ? 2 : 1
        maxCount: isProd ? 10 : 2
        enableAutoScaling: true
        availabilityZones: isProd ? ['1', '2'] : null
        vnetSubnetID: aksSubnetId
        osType: 'Linux'
      }
    ]
    addonProfiles: {
      omsagent: {
        enabled: true
        config: { logAnalyticsWorkspaceResourceID: logAnalyticsId }
      }
      azureKeyvaultSecretsProvider: {
        enabled: true                                       // CSI driver (REQ-INF-062)
        config: { enableSecretRotation: 'true' }
      }
    }
  }
}

// AKS kubelet pulls from ACR
resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, aks.id, 'acrpull')
  properties: {
    principalId: aks.properties.identityProfile.kubeletidentity.objectId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '7f951dda-4ed3-4680-a7ca-43fe172d538d' // AcrPull
    )
  }
}

output aksName string = aks.name
output acrLoginServer string = acr.properties.loginServer
output oidcIssuerUrl string = aks.properties.oidcIssuerProfile.issuerURL
