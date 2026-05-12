"""Long-running worker processes that consume the task table.

Modules under this package implement the *execution* side of the LCP
control plane: they pull tasks via :mod:`lcp.services.scheduler_service`
and run business logic against (eventually) the lance data plane.
"""
