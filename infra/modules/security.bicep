// ============================================================================
// Security module — REQ-INF-060..063
// Key Vault (RBAC mode, soft-delete, purge protection) + private endpoint
// ============================================================================
param location string
param suffix string
param tags object
param adminGroupObjectId string
param dataSubnetId string

resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'kv-${suffix}'
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true      // RBAC mode, not access policies (REQ-INF-061)
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enablePurgeProtection: true        // REQ-INF-063
    publicNetworkAccess: 'Disabled'
    networkAcls: { defaultAction: 'Deny', bypass: 'AzureServices' }
  }
}

// Key Vault Administrator for the platform admin group
resource kvAdmin 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: kv
  name: guid(kv.id, adminGroupObjectId, 'kv-admin')
  properties: {
    principalId: adminGroupObjectId
    principalType: 'Group'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '00482a5a-887f-4fb3-b363-3b7fe8e74483' // Key Vault Administrator
    )
  }
}

resource kvPe 'Microsoft.Network/privateEndpoints@2024-01-01' = {
  name: 'pe-${suffix}-kv'
  location: location
  tags: tags
  properties: {
    subnet: { id: dataSubnetId }
    privateLinkServiceConnections: [
      {
        name: 'kv'
        properties: {
          privateLinkServiceId: kv.id
          groupIds: ['vault']
        }
      }
    ]
  }
}

// Register the KV private endpoint into its private DNS zone (REQ-INF-013/015,
// task 1R.1 — mirrors the pattern in data.bicep's peDns resource)
resource kvPeDns 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-01-01' = {
  parent: kvPe
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'kv'
        properties: {
          privateDnsZoneId: resourceId('rg-${suffix}-network', 'Microsoft.Network/privateDnsZones', 'privatelink.vaultcore.azure.net')
        }
      }
    ]
  }
}

output keyVaultName string = kv.name
output keyVaultId string = kv.id
