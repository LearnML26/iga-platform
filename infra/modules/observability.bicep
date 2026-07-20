// ============================================================================
// Observability module — REQ-INF-080..084
// Log Analytics + App Insights + core alert rules + action group
// ============================================================================
param location string
param suffix string
param tags object
param isProd bool

resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'law-${suffix}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 90            // hot retention — REQ-INF-081
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${suffix}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: law.id
    IngestionMode: 'LogAnalytics'
  }
}

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'ag-${suffix}-oncall'
  location: 'global'
  tags: tags
  properties: {
    groupShortName: 'iga-oncall'
    enabled: true
    emailReceivers: [
      {
        name: 'platform-team'
        emailAddress: 'iga-oncall@example.com' // replace via parameter or post-deploy
        useCommonAlertSchema: true
      }
    ]
  }
}

// API 5xx error rate alert (REQ-INF-082) — others are added as the services
// emit custom metrics; DLQ alert lives in compute (needs SB metric scope).
resource fiveXxAlert 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: 'alert-${suffix}-api-5xx'
  location: 'global'
  tags: tags
  properties: {
    severity: 2
    enabled: true
    scopes: [appInsights.id]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: '5xx'
          criterionType: 'StaticThresholdCriterion'
          metricName: 'requests/failed'
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: isProd ? 10 : 100
        }
      ]
    }
    actions: [
      { actionGroupId: actionGroup.id }
    ]
  }
}

output logAnalyticsId string = law.id
output appInsightsConnectionString string = appInsights.properties.ConnectionString
