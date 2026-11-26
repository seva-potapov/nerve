import type { SessionProcess } from '../../../types/agents';

/** A process's identity across polls: a PID can be reused, its start time can't. */
export function procKey(p: SessionProcess): string {
  return `${p.pid}:${p.start_ticks}`;
}

export interface ProcessTreeRow {
  proc: SessionProcess;
  /** 0 for a row whose parent is not in the list. */
  depth: number;
  /** Its children in the list. */
  childCount: number;
  /** How many rows are hidden under it while it is collapsed (else 0). */
  hidden: number;
}

/**
 * One group's processes as a forest, in display order: each process under
 * its parent (`ppid`) when the parent is in the same list, siblings in the
 * list's order (the server sorts by start time, then PID). The subtree of a
 * key in `collapsed` is left out and counted on its row. Every process is
 * shown once, even if the parent links form a cycle.
 */
export function processTree(procs: SessionProcess[], collapsed: ReadonlySet<string> = new Set()): ProcessTreeRow[] {
  const byPid = new Map<number, SessionProcess>();
  for (const p of procs) if (!byPid.has(p.pid)) byPid.set(p.pid, p);
  const children = new Map<number, SessionProcess[]>();
  const roots: SessionProcess[] = [];
  for (const p of procs) {
    if (p.ppid !== p.pid && byPid.has(p.ppid)) {
      const list = children.get(p.ppid);
      if (list) list.push(p);
      else children.set(p.ppid, [p]);
    } else {
      roots.push(p);
    }
  }
  const rows: ProcessTreeRow[] = [];
  const visited = new Set<SessionProcess>();
  // The size of the subtree at `p` (itself included); its rows are added
  // only while `show` holds.
  const walk = (p: SessionProcess, depth: number, show: boolean): number => {
    if (visited.has(p)) return 0;
    visited.add(p);
    const kids = children.get(p.pid) ?? [];
    const row: ProcessTreeRow = { proc: p, depth, childCount: kids.length, hidden: 0 };
    if (show) rows.push(row);
    const fold = show && kids.length > 0 && collapsed.has(procKey(p));
    let below = 0;
    for (const kid of kids) below += walk(kid, depth + 1, show && !fold);
    if (fold) row.hidden = below;
    return 1 + below;
  };
  for (const root of roots) walk(root, 0, true);
  // A parent cycle has no root: show what is left at the top level.
  for (const p of procs) walk(p, 0, true);
  return rows;
}
