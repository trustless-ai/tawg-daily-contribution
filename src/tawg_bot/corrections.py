"""Deterministic boundary for current-knowledge and local-alias corrections."""

from __future__ import annotations

from tawg_bot.unit_of_work import RepositoryUnitOfWork
from tawg_bot.vault_transaction import VaultTransaction, VaultTransactionEngine


class CorrectionRejected(ValueError):
    """Raised when a proposed correction is not a current-knowledge replacement."""


class CorrectionService:
    def __init__(self, engine: VaultTransactionEngine) -> None:
        self.engine = engine

    def stage(
        self,
        transaction: VaultTransaction,
        *,
        operation_id: str,
        uow: RepositoryUnitOfWork,
    ) -> tuple[str, ...]:
        if transaction.operation_id != operation_id:
            raise CorrectionRejected("correction operation_id mismatch")
        for write in transaction.writes:
            name = write.path.casefold()
            if "correction" in name or "changelog" in name:
                raise CorrectionRejected("corrections replace current knowledge in place")
        inspection = self.engine.inspect(transaction)
        self.engine.stage(transaction, inspection.approval_sha256, uow)
        return inspection.changed_paths
