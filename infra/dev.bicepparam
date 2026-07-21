using 'main.bicep'

param environment = 'dev'
param location = 'eastus'
param appName = 'iga'
// Replace with your Entra ID admin group objectId before deploying:
param adminGroupObjectId = '00000000-0000-0000-0000-000000000000'
