import { Fragment, type ReactNode } from 'react'
import { useActiveConnectionScope } from '../api/connections'

/** Editing state, selections and pending UI responses belong to one Brain configuration. */
export default function BrainScope({ children }: { children: ReactNode }) {
  const scope = useActiveConnectionScope()
  return <Fragment key={scope}>{children}</Fragment>
}
