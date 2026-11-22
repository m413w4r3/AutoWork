"""UoW wiring oracle (ADR-0011 E2): protocol, SQLAlchemy and fake together.

Lot 02 adds exactly four workspace repositories. They must appear once in the
``UnitOfWork`` protocol, once in ``SqlAlchemyUnitOfWork`` (annotation and one
assignment), and have a usable in-memory fake in the same module as every other
investigation port.
"""

from __future__ import annotations

import inspect
import sys

from cti_app.application import investigation_persistence as ports
from cti_app.application.persistence import UnitOfWork
from cti_app.infrastructure.database import uow as uow_module
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork

EXPECTED_REPOSITORIES = {
    "workspace_bindings": (
        ports.WorkspaceBindingRepository,
        ports.InMemoryWorkspaceBindingRepository,
        uow_module.SqlAlchemyWorkspaceBindingRepository,
    ),
    "workspace_file_revisions": (
        ports.WorkspaceFileRevisionRepository,
        ports.InMemoryWorkspaceFileRevisionRepository,
        uow_module.SqlAlchemyWorkspaceFileRevisionRepository,
    ),
    "workspace_file_events": (
        ports.WorkspaceFileEventRepository,
        ports.InMemoryWorkspaceFileEventRepository,
        uow_module.SqlAlchemyWorkspaceFileEventRepository,
    ),
    "workspace_sync_cursors": (
        ports.WorkspaceSyncCursorRepository,
        ports.InMemoryWorkspaceSyncCursorRepository,
        uow_module.SqlAlchemyWorkspaceSyncCursorRepository,
    ),
    # Lot 04A1 follows the workspace repositories in the total migration order.
    "investigation_plans": (
        ports.InvestigationPlanRepository,
        ports.InMemoryInvestigationPlanRepository,
        uow_module.SqlAlchemyInvestigationPlanRepository,
    ),
    # Lot 04B1 follows 04A1 in the total migration order.
    "investigation_tasks": (
        ports.InvestigationTaskRepository,
        ports.InMemoryInvestigationTaskRepository,
        uow_module.SqlAlchemyInvestigationTaskRepository,
    ),
    # Lot 05A follows 04B1 in the total migration order.
    "investigation_handoffs": (
        ports.InvestigationHandoffRepository,
        ports.InMemoryInvestigationHandoffRepository,
        uow_module.SqlAlchemyInvestigationHandoffRepository,
    ),
    # Lot 05B1 follows 05A in the total migration order.
    "agent_sessions": (
        ports.AgentSessionRepository,
        ports.InMemoryAgentSessionRepository,
        uow_module.SqlAlchemyAgentSessionRepository,
    ),
    # Lot 07 follows 05B1 in the total migration order.
    "work_contributions": (
        ports.WorkContributionRepository,
        ports.InMemoryWorkContributionRepository,
        uow_module.SqlAlchemyWorkContributionRepository,
    ),
    # Lot 08B follows 07 in the total migration order.
    "investigation_note_proposals": (
        ports.NoteProposalRepository,
        ports.InMemoryNoteProposalRepository,
        uow_module.SqlAlchemyNoteProposalRepository,
    ),
    # M24-WS2 (notes) follows 08B in the total migration order.
    "workspace_note_annotations": (
        ports.WorkspaceNoteAnnotationRepository,
        ports.InMemoryWorkspaceNoteAnnotationRepository,
        uow_module.SqlAlchemyWorkspaceNoteAnnotationRepository,
    ),
}


def _resolved_annotation(owner: type, name: str) -> object:
    annotation = owner.__annotations__[name]
    if not isinstance(annotation, str):
        return annotation
    module_globals = vars(sys.modules[owner.__module__])
    return eval(annotation, module_globals)


def test_new_investigation_repositories_are_wired_once() -> None:
    enter_source = inspect.getsource(SqlAlchemyUnitOfWork.__aenter__)
    for name, (protocol, _fake, adapter) in EXPECTED_REPOSITORIES.items():
        assert _resolved_annotation(UnitOfWork, name) is protocol, name
        assert _resolved_annotation(SqlAlchemyUnitOfWork, name) is protocol, name
        assert enter_source.count(f"self.{name} = ") == 1, name
        assert f"self.{name} = {adapter.__name__}(" in enter_source, name


def test_memory_fakes_implement_every_protocol_method() -> None:
    for name, (protocol, fake, _) in EXPECTED_REPOSITORIES.items():
        for method_name, member in vars(protocol).items():
            if method_name.startswith("_"):
                continue
            assert callable(member), (name, method_name)
            assert callable(getattr(fake, method_name, None)), (name, method_name)
        assert fake.__name__ in ports.__all__, name


def test_workspace_ports_are_exported_from_the_investigation_module() -> None:
    for name, (protocol, _, _) in EXPECTED_REPOSITORIES.items():
        assert protocol.__name__ in ports.__all__, name
        assert getattr(ports, protocol.__name__) is protocol
