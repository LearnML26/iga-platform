// ============================================================================
// Messaging module — REQ-INF-050..053
// Service Bus (task queues + DLQ + sessions), Event Hubs (identity changes)
// ============================================================================
param location string
param suffix string
param tags object
param isProd bool
param dataSubnetId string

resource sb 'Microsoft.ServiceBus/namespaces@2024-01-01' = {
  name: 'sb-${suffix}'
  location: location
  tags: tags
  sku: isProd
    ? { name: 'Premium', tier: 'Premium', capacity: 1 }
    : { name: 'Standard', tier: 'Standard' }
  properties: {
    minimumTlsVersion: '1.2'
    disableLocalAuth: true // Entra auth only
    publicNetworkAccess: isProd ? 'Disabled' : 'Enabled'
  }
}

// Session-enabled provisioning queue guarantees per-identity+target ordering
// (REQ-INF-053 / REQ-COR-PROV-002)
var queues = [
  { name: 'provisioning-tasks',  sessions: true  }
  { name: 'provisioning-retry',  sessions: true  }
  { name: 'certification-tasks', sessions: false }
  { name: 'notification-tasks',  sessions: false }
]

resource q 'Microsoft.ServiceBus/namespaces/queues@2024-01-01' = [for item in queues: {
  parent: sb
  name: item.name
  properties: {
    requiresSession: item.sessions
    deadLetteringOnMessageExpiration: true
    maxDeliveryCount: 5              // REQ-COR-PROV-003 default attempt cap
    lockDuration: 'PT5M'
    defaultMessageTimeToLive: 'P7D'
  }
}]

resource eh 'Microsoft.EventHub/namespaces@2024-01-01' = {
  name: 'evh-${suffix}'
  location: location
  tags: tags
  sku: { name: 'Standard', tier: 'Standard', capacity: 1 }
  properties: {
    minimumTlsVersion: '1.2'
    disableLocalAuth: true
    zoneRedundant: isProd
  }
}

resource identityChanges 'Microsoft.EventHub/namespaces/eventhubs@2024-01-01' = {
  parent: eh
  name: 'identity-changes'
  properties: {
    partitionCount: 4
    retentionDescription: {
      cleanupPolicy: 'Delete'
      retentionTimeInHours: 168   // 7 days — REQ-INF-051
    }
  }
}

resource rulesConsumer 'Microsoft.EventHub/namespaces/eventhubs/consumergroups@2024-01-01' = {
  parent: identityChanges
  name: 'rules-engine'
}

resource auditConsumer 'Microsoft.EventHub/namespaces/eventhubs/consumergroups@2024-01-01' = {
  parent: identityChanges
  name: 'audit-service'
}

// ---------------------------------------------------------------------------
// Private endpoints (REQ-INF-013) — 1R.2: previously missing entirely.
// publicNetworkAccess is left as-is (Enabled in dev) for this task; disabling
// it for namespaces that get a PE is a deliberate follow-up once the private
// path here is verified, since identity-service/provisioning-service depend
// on live connectivity to these namespaces today.
//
// Service Bus is EXCLUDED: Azure only supports private endpoints on Premium
// Service Bus namespaces (confirmed via failed deployment,
// PrivateEndpointInvalidSku), and CLAUDE.md forbids Premium SKUs in dev.
// sb-${suffix} (Standard tier) therefore cannot be network-isolated in dev
// at all — this is an Azure platform constraint, not a config gap. It stays
// on public access with Entra-only data-plane auth (disableLocalAuth) as its
// sole access control until a decision is made to run Premium in dev or
// accept this gap through to prod (prod already provisions Premium).
// ---------------------------------------------------------------------------
var messagingPrivateEndpoints = [
  { name: 'eventhub', resourceId: eh.id, groupId: 'namespace' }
]

resource messagingPes 'Microsoft.Network/privateEndpoints@2024-01-01' = [for pe in messagingPrivateEndpoints: {
  name: 'pe-${suffix}-${pe.name}'
  location: location
  tags: tags
  properties: {
    subnet: { id: dataSubnetId }
    privateLinkServiceConnections: [
      {
        name: pe.name
        properties: {
          privateLinkServiceId: pe.resourceId
          groupIds: [pe.groupId]
        }
      }
    ]
  }
}]

// Service Bus and Event Hubs both resolve through privatelink.servicebus.windows.net
resource messagingPeDns 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-01-01' = [for (pe, i) in messagingPrivateEndpoints: {
  parent: messagingPes[i]
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: pe.name
        properties: {
          privateDnsZoneId: resourceId('rg-${suffix}-network', 'Microsoft.Network/privateDnsZones', 'privatelink.servicebus.windows.net')
        }
      }
    ]
  }
}]

output serviceBusNamespace string = sb.name
output eventHubNamespace string = eh.name
