// ============================================================================
// Messaging module — REQ-INF-050..053
// Service Bus (task queues + DLQ + sessions), Event Hubs (identity changes)
// ============================================================================
param location string
param suffix string
param tags object
param isProd bool

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

output serviceBusNamespace string = sb.name
output eventHubNamespace string = eh.name
