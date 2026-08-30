/**
 * The application shell.
 *
 * Proportions and composition: a 54 px bar carrying the wordmark, the host it is
 * looking at, the navigation, a freshness stamp and the account menu — with the app bar's
 * single `backdrop-filter`, which is the only glass effect in the product.
 *
 * The bar is the only navigation layer. The design direction also carries a search palette,
 * a "Run" control and a live-observation toggle; none is present here. Search over objects
 * has no backing endpoint, "Run" opens a composer for guarded operations this read-only
 * console does not expose pending authentication, and a "live" indicator would claim a
 * polling behaviour this build does not perform. Each is omitted rather than drawn as an
 * inert affordance.
 *
 * The layout uses the full viewport width. LocalPlane is a dense infrastructure interface and
 * a wide display should carry more rows and more columns, not the same narrow column with
 * larger margins. Prose blocks keep their own max-width so line length stays readable.
 */
import { NavLink, Outlet } from 'react-router-dom';
import { DomainNav, type Domain } from './DomainNav';
import { useEstateCounts } from '@/hooks/useEstateCounts';
import { AppearanceSelect } from './AppearanceSelect';
import { HostScope } from './HostScope';
import { ReadStamp } from './ReadStamp';
import styles from './AppShell.module.css';

/**
 * The five surfaces.
 *
 * Ordered as the workflow reads: see the whole, then the two things a host is made of, then
 * what it is running, then what has been done to it.
 */
/**
 * The domains, and what each contains.
 *
 * Only real destinations appear. Uplinks, Routes, DNS, Firewall, Neighbours and
 * Traffic belong under Network, and Kernel, Packages, Users, Logs and Time under System;
 * none has a backend contract in this build, and a menu entry leading nowhere is worse than
 * an absent one. The first entry of each domain is that domain's own surface.
 */
function DOMAINS(counts: ReturnType<typeof useEstateCounts>): readonly Domain[] {
  return [
    {
      id: 'overview',
      label: 'Overview',
      to: '/',
      priority: 1,
      entries: [
        {
          to: '/',
          label: 'Overview',
          root: true,
          description: 'The machine, what wants attention, and what it is running',
        },
      ],
    },
    {
      id: 'network',
      label: 'Network',
      to: '/network',
      priority: 2,
      count: counts.interfaces,
      entries: [
        {
          to: '/network',
          label: 'Interfaces',
          root: true,
          count: counts.interfaces,
          description:
            'Ports, bridges and tunnels — what each is, who configures it, and what it is attached to',
        },
      ],
    },
    {
      id: 'workloads',
      label: 'Workloads',
      to: '/workloads',
      priority: 3,
      count: counts.applications,
      entries: [
        {
          to: '/workloads',
          label: 'Applications',
          root: true,
          count: counts.applications,
          description:
            'Compose projects and standalone containers — the things this host runs on your behalf',
        },
        {
          to: '/workloads/runtime',
          label: 'Runtime',
          description:
            'The engine behind these workloads, the images they run from, and which runtimes are observed',
        },
      ],
    },
    {
      id: 'system',
      label: 'System',
      to: '/system',
      priority: 4,
      count: counts.units,
      entries: [
        {
          to: '/system',
          label: 'Units',
          root: true,
          count: counts.units,
          description:
            'The system manager’s loaded estate — services, sockets, timers, mounts and targets',
        },
        {
          to: '/system?type=service',
          label: 'Services',
          count: counts.services,
          description: 'Just the service units',
        },
      ],
    },
    {
      id: 'operations',
      label: 'Operations',
      to: '/operations',
      priority: 5,
      count: counts.changes,
      attention: (counts.findings ?? 0) > 0,
      entries: [
        {
          to: '/operations',
          label: 'Runs and changes',
          root: true,
          count: counts.changes,
          description: 'What was planned, what crossed the write boundary, and how each ended',
        },
        {
          to: '/operations/findings',
          label: 'Findings',
          count: counts.findings,
          description:
            'Durable claims LocalPlane is making about this host — drift and ownership conflicts',
        },
      ],
    },
  ];
}

export function AppShell(): JSX.Element {
  const counts = useEstateCounts();

  return (
    <div className={styles.shell}>
      <a href="#main" className={styles.skip}>
        Skip to content
      </a>

      <header className={styles.appbar}>
        <div className={styles.barLeft}>
          <NavLink to="/" className={styles.brand ?? ''} title="LocalPlane — Overview">
            Local<i>Plane</i>
          </NavLink>
          <HostScope />
        </div>

        <DomainNav domains={DOMAINS(counts)} />

        <div className={styles.barRight}>
          <ReadStamp />
          <AppearanceSelect />
        </div>
      </header>

      <main id="main" className={styles.main}>
        <Outlet />
      </main>
    </div>
  );
}
