# QualityFlow Formal Console UI Design

## Goal

Turn the existing lightweight console into a credible enterprise test-operations control plane without changing backend APIs, adding a frontend framework, or hiding the platform's student-scale limitations.

## Direction

Use a dark, data-dense operations aesthetic: near-black navy canvas, restrained blue-green accent, low-elevation panels, crisp borders, compact typography, and one consistent family of inline SVG icons. The interface should feel like a maintained internal platform rather than a landing page.

## Information architecture

- A persistent sidebar provides product identity, navigation, environment label, and live platform readiness.
- The runs view starts with four operational KPIs derived from the already-loaded run list: total visible runs, currently running, passed, and attention required. Filters and the run table remain the primary work surface.
- The create view uses a two-column execution workspace on desktop: configuration and request data on the left, a sticky execution summary and safety boundary on the right. On narrow screens it becomes one column.
- The detail view keeps the existing evidence panels but improves hierarchy with a compact breadcrumb, terminal-state header, KPI summary, and consistent section treatment.
- The health view presents live and readiness checks as operational service cards with explicit endpoint labels and guidance.

## Interaction and states

- Existing API routes, IDs, form behavior, polling, artifact links, and validation stay compatible.
- Navigation and buttons use semantic SVG icons, visible focus rings, disabled/loading states, and 44px minimum targets.
- Table rows remain horizontally scrollable on small screens. At phone width the sidebar becomes a compact top navigation and form/sidebar grids collapse.
- Reduced-motion preferences disable reveal and hover movement.
- Empty, loading, error, ready, and validation states use text plus shape/icon, never color alone.

## Trust boundary

The UI continues to expose only registered suites, allowlisted parameters, safe request-body schemas/examples, and public run evidence. It must not expose commands, filesystem paths, Redis/PostgreSQL credentials, or imply production-grade authentication or isolation.

## Verification

- Extend the UI contract test for formal console landmarks, KPI elements, SVG icon system, and execution-summary panel.
- Run JavaScript syntax checks, Ruff, console unit tests, and the full unit suite.
- Visually inspect runs, create, detail, and health states at desktop, tablet, and 375px widths; verify keyboard focus and reduced-motion CSS.

