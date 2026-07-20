// ============================================================================
// Network module — REQ-INF-010..016
// Spoke VNet with segmented subnets and default-deny NSGs.
// (Hub VNet / Firewall / ExpressRoute assumed to exist at the org level;
//  peering is wired post-deploy by the connectivity team.)
// ============================================================================
param location string
param suffix string
param tags object

var vnetCidr = '10.20.0.0/16'

resource nsgApp 'Microsoft.Network/networkSecurityGroups@2024-01-01' = {
  name: 'nsg-${suffix}-appgw'
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: 'allow-https-inbound'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'Internet'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '443'
        }
      }
      {
        // Required for Application Gateway v2 platform management traffic
        name: 'allow-gwm-inbound'
        properties: {
          priority: 110
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'GatewayManager'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '65200-65535'
        }
      }
    ]
  }
}

resource nsgAks 'Microsoft.Network/networkSecurityGroups@2024-01-01' = {
  name: 'nsg-${suffix}-aks'
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: 'allow-appgw-to-aks'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: '10.20.1.0/24'
          sourcePortRange: '*'
          destinationAddressPrefix: '10.20.4.0/22'
          destinationPortRanges: ['443', '80']
        }
      }
    ]
  }
}

resource nsgData 'Microsoft.Network/networkSecurityGroups@2024-01-01' = {
  name: 'nsg-${suffix}-data'
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: 'allow-aks-to-data'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: '10.20.4.0/22'
          sourcePortRange: '*'
          destinationAddressPrefix: '10.20.8.0/24'
          destinationPortRange: '443'
        }
      }
      {
        name: 'allow-aks-to-sql'
        properties: {
          priority: 110
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: '10.20.4.0/22'
          sourcePortRange: '*'
          destinationAddressPrefix: '10.20.8.0/24'
          destinationPortRange: '1433'
        }
      }
    ]
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-01-01' = {
  name: 'vnet-${suffix}-spoke'
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: [vnetCidr] }
    subnets: [
      {
        name: 'snet-appgw'
        properties: {
          addressPrefix: '10.20.1.0/24'
          networkSecurityGroup: { id: nsgApp.id }
        }
      }
      {
        name: 'snet-apim'
        properties: {
          addressPrefix: '10.20.2.0/24'
        }
      }
      {
        name: 'snet-integration'
        properties: {
          addressPrefix: '10.20.3.0/24'
          privateEndpointNetworkPolicies: 'Enabled'
        }
      }
      {
        name: 'snet-aks'
        properties: {
          addressPrefix: '10.20.4.0/22'
          networkSecurityGroup: { id: nsgAks.id }
        }
      }
      {
        name: 'snet-data'
        properties: {
          addressPrefix: '10.20.8.0/24'
          networkSecurityGroup: { id: nsgData.id }
          privateEndpointNetworkPolicies: 'Enabled'
        }
      }
    ]
  }
}

// Private DNS zones for Private Link resolution (REQ-INF-015)
var privateDnsZones = [
  'privatelink.documents.azure.com'       // Cosmos DB
  'privatelink${az.environment().suffixes.sqlServerHostname}' // Azure SQL
  'privatelink.blob.${az.environment().suffixes.storage}'     // Blob/ADLS
  'privatelink.dfs.${az.environment().suffixes.storage}'      // ADLS Gen2
  'privatelink.vaultcore.azure.net'       // Key Vault
  'privatelink.servicebus.windows.net'    // Service Bus / Event Hubs
  'privatelink.redis.cache.windows.net'   // Redis
]

resource dnsZones 'Microsoft.Network/privateDnsZones@2024-06-01' = [for zone in privateDnsZones: {
  name: zone
  location: 'global'
  tags: tags
}]

resource dnsLinks 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = [for (zone, i) in privateDnsZones: {
  parent: dnsZones[i]
  name: 'link-${suffix}'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: vnet.id }
  }
}]

output vnetId string = vnet.id
output aksSubnetId string = vnet.properties.subnets[3].id
output dataSubnetId string = vnet.properties.subnets[4].id
output integrationSubnetId string = vnet.properties.subnets[2].id
