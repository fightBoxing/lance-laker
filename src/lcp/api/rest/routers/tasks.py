"""Tasks router.

Mirrors ``docs/architecture/api/openapi/lcp-task-api.yaml``.  Persistence and
state-machine logic live in :mod:`lcp.services.task_service`; the router only
wires HTTP to the service layer.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from lcp.api.rest.deps import PrincipalDep, SessionDep
from lcp.core.tenant import TenantPrincipal
from lcp.schemas.task import TaskListResponse, TaskResponse, TaskSubmitRequest
from lcp.services import task_service

router = APIRouter(prefix="/v1/tasks", tags=["tasks"])


def _not_found(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "NOT_FOUND", "message": message},
    )


def _conflict(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": code, "message": message},
    )


@router.get("", response_model=TaskListResponse, summary="List tasks")
async def list_tasks(
    session: Annotated[AsyncSession, SessionDep],
    _principal: Annotated[TenantPrincipal, PrincipalDep],
    dataset_uuid: Annotated[str | None, Query()] = None,
    task_type: Annotated[str | None, Query(alias="type")] = None,
    task_status: Annotated[str | None, Query(alias="status")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 20,
) -> TaskListResponse:
    """List background tasks for the caller's tenant."""

    items, total = await task_service.list_tasks(
        session,
        dataset_uuid=dataset_uuid,
        task_type=task_type,
        status=task_status,
        page=page,
        page_size=page_size,
    )
    return TaskListResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=[TaskResponse.model_validate(it) for it in items],
    )


@router.post(
    "",
    response_model=TaskResponse,
    summary="Submit task",
    responses={
        202: {"description": "Accepted"},
        200: {"description": "Idempotent replay; existing task returned"},
    },
)
async def submit_task(
    payload: TaskSubmitRequest,
    session: Annotated[AsyncSession, SessionDep],
    _principal: Annotated[TenantPrincipal, PrincipalDep],
    response: Response,
) -> TaskResponse:
    """Submit a new task; ``202 Accepted`` on first call, ``200 OK`` on replay."""

    obj, created = await task_service.submit_task(
        session,
        task_type=payload.task_type,
        dataset_uuid=payload.dataset_uuid,
        priority=payload.priority,
        params=payload.params,
        idempotency_key=payload.idempotency_key,
        max_attempts=payload.max_attempts,
    )
    response.status_code = (
        status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK
    )
    return TaskResponse.model_validate(obj)


@router.get(
    "/{task_uuid}",
    response_model=TaskResponse,
    summary="Get task status",
)
async def get_task(
    task_uuid: str,
    session: Annotated[AsyncSession, SessionDep],
    _principal: Annotated[TenantPrincipal, PrincipalDep],
) -> TaskResponse:
    """Return progress and metadata for a single task."""

    try:
        obj = await task_service.get_task(session, task_uuid)
    except task_service.TaskNotFoundError as exc:
        raise _not_found(f"task {task_uuid} not found") from exc
    return TaskResponse.model_validate(obj)


@router.post(
    "/{task_uuid}/cancel",
    response_model=TaskResponse,
    summary="Cancel task",
)
async def cancel_task(
    task_uuid: str,
    session: Annotated[AsyncSession, SessionDep],
    _principal: Annotated[TenantPrincipal, PrincipalDep],
) -> TaskResponse:
    """Best-effort cancellation; idempotent on already-cancelled tasks."""

    try:
        obj = await task_service.cancel_task(session, task_uuid)
    except task_service.TaskNotFoundError as exc:
        raise _not_found(f"task {task_uuid} not found") from exc
    except task_service.TaskTransitionError as exc:
        raise _conflict("INVALID_TRANSITION", str(exc)) from exc
    return TaskResponse.model_validate(obj)


@router.post(
    "/{task_uuid}/retry",
    response_model=TaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Retry failed task",
)
async def retry_task(
    task_uuid: str,
    session: Annotated[AsyncSession, SessionDep],
    _principal: Annotated[TenantPrincipal, PrincipalDep],
) -> TaskResponse:
    """Re-queue a FAILED task; rejected on non-FAILED states."""

    try:
        obj = await task_service.retry_task(session, task_uuid)
    except task_service.TaskNotFoundError as exc:
        raise _not_found(f"task {task_uuid} not found") from exc
    except task_service.TaskTransitionError as exc:
        raise _conflict("INVALID_TRANSITION", str(exc)) from exc
    return TaskResponse.model_validate(obj)
