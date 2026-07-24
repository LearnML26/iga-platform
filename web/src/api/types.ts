/** Shapes mirrored from the service Pydantic models — kept minimal (only the
 * fields the UI reads); services may return more, which is fine. */

export interface Identity {
  identityId: string;
  correlationKey: string;
  displayName: string;
  status: string;
  department?: string | null;
  jobTitle?: string | null;
  managerIdentityId?: string | null;
  startDate?: string | null;
  terminationDate?: string | null;
  createdDate?: string;
  lastModifiedDate?: string;
  [k: string]: unknown;
}

export interface HistoryEvent {
  id: string;
  identityId: string;
  eventType: string;
  actor: string;
  timestamp: string;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
}

export interface SourceSystem {
  id: string;
  name: string;
  connectorType: string;
  description?: string | null;
  status: string;
  provisioningTargets: string[];
  ownerIdentityId?: string | null;
  createdDate: string;
}

export interface FeedRun {
  id: string;
  sourceSystemInstanceId: string;
  status: string;
  triggeredBy: string;
  startedAt: string;
  completedAt?: string | null;
  recordsProcessed: number;
  recordsAdded: number;
  recordsUpdated: number;
  recordsTerminated: number;
  recordsQuarantined: number;
  errorSummary?: string | null;
}

export interface ProvisioningTaskRecord {
  taskId: string;
  sourceType: string;
  sourceRef: string;
  identityId: string;
  instanceId: string;
  connectorType: string;
  operationType: string;
  entitlementRef?: string | null;
  status: string;
  attemptCount: number;
  lastError?: string | null;
  nextAttemptAt?: string | null;
  createdDate?: string | null;
}

export interface RoleAssignmentView {
  id: string;
  roleId: string;
  roleName: string;
  roleDescription?: string | null;
  assignmentType: string;
  status: string;
  createdDate: string;
  entitlements: { targetSystemInstanceId: string; connectorType: string; entitlementRef: string }[];
}

export interface ApprovalStepView {
  id: string;
  lineItemId: string;
  requestId: string;
  stepType: string;
  status: string;
  actionable: boolean;
  requesterIdentityId: string;
  targetSystemInstanceId: string;
  connectorType: string;
  entitlementRef: string;
  justification?: string | null;
  createdDate: string;
}

export interface AccessRequest {
  id: string;
  requesterIdentityId: string;
  status: string;
  createdDate: string;
  lineItems: {
    id: string;
    targetSystemInstanceId: string;
    connectorType: string;
    entitlementRef: string;
    justification?: string | null;
    status: string;
    approvalSteps: {
      id: string;
      sequenceOrder: number;
      stepType: string;
      approverIdentityId?: string | null;
      status: string;
    }[];
  }[];
}
