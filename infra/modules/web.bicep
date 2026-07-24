// ============================================================================
// Web module — Static Web App hosting for the React frontend (Phase 3.4,
// REQ-UI-001..005).
//
// Region note: Static Web Apps only accepts a fixed set of metadata regions
// (westus2, centralus, eastus2, westeurope, eastasia) — eastus is not one of
// them, so this module defaults to eastus2. Same class of documented
// exception as sql-iga-dev living in canadacentral: the constraint is
// Azure's, not ours, and content is served from SWA's global edge regardless
// of the metadata region.
//
// Deliberate scope limit (flagged in roadmap/PHASES.md 3.4): the SWA hosts
// static assets only. It has no line of sight to the ClusterIP services in
// the AKS VNet, so the publicly-hosted SPA cannot reach the APIs until
// Phase 4.5 puts APIM in front of them — that is the roadmap's own
// sequencing, not an oversight. Until then the fully-functional path is
// local dev via scripts/dev-portal.sh.
//
// Content deploys are HUMAN-run (`swa deploy` needs the deployment token, a
// secret this repo's guardrails forbid handling) — deploy.sh prints the
// exact steps.
// ============================================================================
param suffix string
param tags object
@description('SWA metadata region — eastus is not in the allowed set')
param location string = 'eastus2'

resource swa 'Microsoft.Web/staticSites@2023-12-01' = {
  name: 'swa-${suffix}-web'
  location: location
  tags: tags
  sku: {
    name: 'Free'   // dev cost ceiling (CLAUDE.md guardrail #4): Free tier
    tier: 'Free'
  }
  properties: {
    stagingEnvironmentPolicy: 'Disabled'
    allowConfigFileUpdates: true
  }
}

output staticWebAppName string = swa.name
output defaultHostname string = swa.properties.defaultHostname
