/**
 * Routes.
 *
 * Five surfaces plus settings, each with a deep-linkable detail route so an operator can send
 * somebody a link to a unit, an interface, a run or a change.
 */
import { Navigate, Route, Routes } from 'react-router-dom';
import { AppShell } from '@/components/layout/AppShell';
import { Overview } from '@/routes/Overview';
import { NetworkList } from '@/routes/network/NetworkList';
import { InterfaceDetail } from '@/routes/network/InterfaceDetail';
import { WorkloadList } from '@/routes/workloads/WorkloadList';
import { WorkloadDetail } from '@/routes/workloads/WorkloadDetail';
import { SystemList } from '@/routes/system/SystemList';
import { UnitDetail } from '@/routes/system/UnitDetail';
import { Operations } from '@/routes/operations/Operations';
import { Findings } from '@/routes/operations/Findings';
import { FindingDetail } from '@/routes/operations/FindingDetail';
import { WorkloadRuntime } from '@/routes/workloads/WorkloadRuntime';
import { RunDetail } from '@/routes/operations/RunDetail';
import { ChangeDetail } from '@/routes/operations/ChangeDetail';
import { Settings } from '@/routes/Settings';
import { NotFound } from '@/routes/NotFound';

export function App(): JSX.Element {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<Overview />} />

        <Route path="network" element={<NetworkList />} />
        <Route path="network/:objectId" element={<InterfaceDetail />} />

        <Route path="workloads" element={<WorkloadList />} />
        <Route path="workloads/runtime" element={<WorkloadRuntime />} />
        <Route path="workloads/:objectId" element={<WorkloadDetail />} />

        <Route path="system" element={<SystemList />} />
        <Route path="system/:objectId" element={<UnitDetail />} />

        <Route path="operations" element={<Operations />} />
        <Route path="operations/findings" element={<Findings />} />
        <Route path="operations/findings/:findingId" element={<FindingDetail />} />
        <Route path="operations/runs/:runId" element={<RunDetail />} />
        <Route path="operations/changes/:changeId" element={<ChangeDetail />} />

        <Route path="settings" element={<Settings />} />

        <Route path="index.html" element={<Navigate to="/" replace />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}
