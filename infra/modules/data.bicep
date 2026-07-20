// ============================================================================
// Data module — REQ-INF-040..046, REQ-INF-013 (private endpoints)
// Azure SQL (db-per-service), Cosmos DB (identity store), ADLS Gen2, Redis
// ============================================================================
param location string
param suffix string
param tags object
param isProd bool
param dataSubnetId string
param logAnalyticsId string
param adminGroupObjectId string

@description('SQL lives in canadacentral: eastus/eastus2 are not accepting new SQL server creation on this subscription')
param sqlLocation string = 'canadacentral'

var cleanSuffix = replace(suffix, '-', '')

// ---------------------------------------------------------------------------
// Azure SQL — one logical server, one DB per relational microservice (REQ-INF-040)
// ---------------------------------------------------------------------------
@description('Entra-only auth: no SQL logins. Set your admin group post-deploy if needed.')
resource sqlServer 'Microsoft.Sql/servers@2023-08-01-preview' = {
  name: 'sql-${suffix}'
  location: sqlLocation
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    publicNetworkAccess: 'Disabled'
    minimalTlsVersion: '1.2'
    administrators: {
      administratorType: 'ActiveDirectory'
      azureADOnlyAuthentication: true
      login: 'iga-sql-admins'
      sid: adminGroupObjectId
      tenantId: subscription().tenantId
    }
  }
}

var serviceDatabases = [
  'targetsystem'
  'sourcesystem'
  'rbac'
  'accessrequest'
  'certification'
  'provisioning'
  'rules'
]

resource sqlDbs 'Microsoft.Sql/servers/databases@2023-08-01-preview' = [for db in serviceDatabases: {
  parent: sqlServer
  name: 'sqldb-${db}'
  location: sqlLocation
  tags: tags
  sku: isProd
    ? { name: 'GP_Gen5_2', tier: 'GeneralPurpose' }
    : { name: 'GP_S_Gen5_1', tier: 'GeneralPurpose' } // serverless in non-prod
  properties: {
    zoneRedundant: isProd
    autoPauseDelay: isProd ? -1 : 60
    minCapacity: isProd ? json('2') : json('0.5')
  }
}]

// ---------------------------------------------------------------------------
// Cosmos DB — identity profile store + entitlement catalog (REQ-INF-041)
// ---------------------------------------------------------------------------
resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-05-15' = {
  name: 'cosmos-${suffix}'
  location: location
  tags: tags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    publicNetworkAccess: 'Disabled'
    consistencyPolicy: { defaultConsistencyLevel: 'Session' }
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: isProd
      }
    ]
    backupPolicy: {
      type: 'Continuous'
      continuousModeProperties: { tier: isProd ? 'Continuous30Days' : 'Continuous7Days' }
    }
    disableLocalAuth: true // Entra/AAD auth only — no account keys (REQ-INF-062)
  }
}

resource cosmosDb 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-05-15' = {
  parent: cosmos
  name: 'iga'
  properties: {
    resource: { id: 'iga' }
  }
}

var cosmosContainers = [
  { name: 'identities', pk: '/tenantId' }
  { name: 'identity-history', pk: '/identityId' }
  { name: 'entitlement-catalog', pk: '/instanceId' }
  { name: 'audit-hot', pk: '/eventDate' }
]

resource containers 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = [for c in cosmosContainers: {
  parent: cosmosDb
  name: c.name
  properties: {
    resource: {
      id: c.name
      partitionKey: { paths: [c.pk], kind: 'Hash' }
    }
    options: {
      autoscaleSettings: { maxThroughput: isProd ? 10000 : 1000 }
    }
  }
}]

// ---------------------------------------------------------------------------
// ADLS Gen2 — raw / curated / audit zones (REQ-INF-042, REQ-NFR-021)
// ---------------------------------------------------------------------------
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: take('st${cleanSuffix}lake', 24)
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: isProd ? 'Standard_ZRS' : 'Standard_LRS' }
  properties: {
    isHnsEnabled: true
    publicNetworkAccess: 'Disabled'
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    networkAcls: { defaultAction: 'Deny', bypass: 'AzureServices' }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    deleteRetentionPolicy: { enabled: true, days: 30 }
  }
}

resource zones 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [for z in ['raw', 'curated', 'audit']: {
  parent: blobService
  name: z
  properties: {
    // Immutability policy on 'audit' is applied post-deploy by deploy.sh
    // (version-level WORM requires a follow-up call) — REQ-NFR-021
    publicAccess: 'None'
  }
}]

// ---------------------------------------------------------------------------
// Redis — session/token/entitlement cache (REQ-INF-046)
// ---------------------------------------------------------------------------
resource redis 'Microsoft.Cache/redis@2024-03-01' = {
  name: 'redis-${suffix}'
  location: location
  tags: tags
  properties: {
    sku: isProd
      ? { name: 'Premium', family: 'P', capacity: 1 }
      : { name: 'Standard', family: 'C', capacity: 0 }
    minimumTlsVersion: '1.2'
    publicNetworkAccess: 'Disabled'
    redisConfiguration: {
      'aad-enabled': 'true'
    }
  }
}

// ---------------------------------------------------------------------------
// Private endpoints (REQ-INF-013) — one per data service
// ---------------------------------------------------------------------------
var privateEndpoints = [
  { name: 'sql', resourceId: sqlServer.id, groupId: 'sqlServer', dnsZone: 'privatelink${az.environment().suffixes.sqlServerHostname}' }
  { name: 'cosmos', resourceId: cosmos.id, groupId: 'Sql', dnsZone: 'privatelink.documents.azure.com' }
  { name: 'lake-blob', resourceId: storage.id, groupId: 'blob', dnsZone: 'privatelink.blob.${az.environment().suffixes.storage}' }
  { name: 'lake-dfs', resourceId: storage.id, groupId: 'dfs', dnsZone: 'privatelink.dfs.${az.environment().suffixes.storage}' }
  { name: 'redis', resourceId: redis.id, groupId: 'redisCache', dnsZone: 'privatelink.redis.cache.windows.net' }
]

resource pes 'Microsoft.Network/privateEndpoints@2024-01-01' = [for pe in privateEndpoints: {
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

// ---------------------------------------------------------------------------
// Diagnostics to Log Analytics (REQ-INF-081)
// ---------------------------------------------------------------------------
resource cosmosDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: cosmos
  name: 'diag-cosmos'
  properties: {
    workspaceId: logAnalyticsId
    logs: [
      { categoryGroup: 'audit', enabled: true }
    ]
  }
}

output sqlServerName string = sqlServer.name
output cosmosAccountName string = cosmos.name
output storageAccountName string = storage.name
output redisName string = redis.name

// Register each private endpoint into its private DNS zone (fix: was missing)
resource peDns 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-01-01' = [for (pe, i) in privateEndpoints: {
  parent: pes[i]
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: pe.name
        properties: {
          privateDnsZoneId: resourceId('rg-${suffix}-network', 'Microsoft.Network/privateDnsZones', pe.dnsZone)
        }
      }
    ]
  }
}]
