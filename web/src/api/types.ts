/**
 * Named domain types over the generated OpenAPI tree.
 *
 * Components import from here and never from `schema.d.ts`. That indirection is the seam
 * that keeps a backend contract change compiling into one file instead of a hundred, and it
 * is why the generated file can be regenerated without ceremony.
 */
import type { components } from './schema';

type S = components['schemas'];

/* -------------------------------------------------------------------- status & identity */
export type BackendStatus = S['BackendStatus'];
export type Host = S['Host'];
export type AgentStatus = S['AgentStatus'];
export type AgentIdentity = S['AgentIdentity'];
export type Capabilities = S['Capabilities'];
export type Capability = S['Capability'];
export type ErrorBody = S['ErrorBody'];
export type SessionStatus = S['SessionStatus'];

/* -------------------------------------------------------------------------- observation */
export type Sweep = S['Sweep'];
export type SweepList = S['SweepList'];
export type Observation = S['Observation'];
export type ProviderIssue = S['ProviderIssue'];
export type Freshness = S['Freshness'];

/* ------------------------------------------------------------------------------ network */
export type NetworkInterface = S['NetworkInterface'];
export type NetworkInterfaceList = S['NetworkInterfaceList'];
export type Link = S['Link'];
export type Address = S['Address'];
export type Statistics = S['Statistics'];
export type Ownership = S['Ownership'];
export type OwnershipClaimSummary = S['OwnershipClaimSummary'];
export type AdoptionEligibility = S['AdoptionEligibility'];
export type Intent = S['Intent'];
export type IntentSummary = S['IntentSummary'];
export type ReconciliationResult = S['ReconciliationResult'];
export type ReconciliationState = S['ReconciliationState'];

/* --------------------------------------------------------------- protection & provenance */
export type ObjectProtection = S['ObjectProtection'];
export type ProtectionReasonView = S['ProtectionReasonView'];
export type ManagementPath = S['ManagementPath'];
export type ManagementPathEvidence = S['ManagementPathEvidence'];
export type Provenance = S['Provenance'];
export type OwnershipClaim = S['OwnershipClaim'];
export type ConsultedSource = S['ConsultedSource'];
export type Evidence = S['Evidence'];
export type IntentHistory = S['IntentHistory'];
export type IntentRevision = S['IntentRevision'];

/* ------------------------------------------------------------------------------ systemd */
export type SystemdUnit = S['SystemdUnit'];
export type SystemdUnitList = S['SystemdUnitList'];
export type SystemdRelationship = S['SystemdRelationship'];
export type SystemdServiceFacts = S['SystemdServiceFacts'];
export type SystemdSocketFacts = S['SystemdSocketFacts'];
export type SystemdTimerFacts = S['SystemdTimerFacts'];
export type SystemdJob = S['SystemdJob'];

/* ------------------------------------------------------------------------------- docker */
export type DockerContainer = S['DockerContainer'];
export type DockerContainerList = S['DockerContainerList'];
export type ContainerRuntime = S['ContainerRuntime'];
export type ContainerPort = S['ContainerPort'];
export type ContainerMount = S['ContainerMount'];
export type ContainerNetwork = S['ContainerNetwork'];
export type ContainerStats = S['ContainerStats'];
export type ContainerLogs = S['ContainerLogs'];
export type ContainerLogLine = S['ContainerLogLine'];

/* --------------------------------------------------------------------- runs and changes */
export type Run = S['Run'];
export type RunList = S['RunList'];
export type RunSummary = S['RunSummary'];
export type RunPreview = S['RunPreview'];
export type RunEventView = S['RunEventView'];
export type RunGuard = S['RunGuard'];
export type RunCheckpoint = S['RunCheckpoint'];
export type PlanExecution = S['PlanExecution'];
export type PlanProtection = S['PlanProtection'];
export type PlanRisk = S['PlanRisk'];
export type PlanConfirmation = S['PlanConfirmation'];
export type PlanVerification = S['PlanVerification'];
export type PlanGuard = S['PlanGuard'];
export type PlanRecovery = S['PlanRecovery'];
export type PlanValidity = S['PlanValidity'];
export type PlanRationale = S['PlanRationale'];
export type PlanEvidence = S['PlanEvidence'];
export type PlannedChange = S['PlannedChange'];
export type PlanSystemdLifecycleContext = S['PlanSystemdLifecycleContext'];
export type PlanAuthorization = S['PlanAuthorization'];

export type Change = S['Change'];
export type ChangeList = S['ChangeList'];
export type ChangeSummary = S['ChangeSummary'];
export type ChangeMutation = S['ChangeMutation'];
export type ChangeVerification = S['ChangeVerification'];
export type ChangeRollback = S['ChangeRollback'];
export type ChangeRecovery = S['ChangeRecovery'];

export type Finding = S['Finding'];
export type FindingList = S['FindingList'];
export type FindingEvidence = S['FindingEvidence'];
export type OwnershipFindingEvidence = S['OwnershipFindingEvidence'];

/* -------------------------------------------------------------------------- shared bits */
export type Health = S['Health'];
export type HealthState = S['HealthState'];
export type Management = S['Management'];
export type ManagementState = S['ManagementState'];
export type Identity = S['Identity'];

/**
 * The closed operation vocabulary, mirrored from the request schema's discriminator rather
 * than retyped. A member added to the backend enum becomes a compile error here first.
 */
export type OperationType = S['ContainerLifecycleOperation']['type']
  | S['ReconcileMtuOperation']['type']
  | S['SystemdServiceLifecycleOperation']['type'];
