---
id: lcl-y7rv
status: open
deps: [lcl-c3ik, lcl-7s7u]
links: []
created: 2026-07-05T05:02:55Z
type: chore
priority: 3
assignee: memgrafter
tags: [proxy, slot-routing]
---
# Remove lmcache-proxy.py and its tests — dead code in production

lmcache-proxy.py is not used in production (run-lmcache-proxy-stack.sh runs lmcache-proxy-on-demand.py). The slot routing logic we added to it was wasted effort. Remove the file and its test suite after the same features are implemented in the on-demand proxy.
