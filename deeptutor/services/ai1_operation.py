"""Bounded operation orchestration; caller supplies a restricted Provider.

No default Provider or runtime settings are loaded. Storage continuity is an
external admission prerequisite, never inferred from rows in this database.
"""

import asyncio
import copy
import re
import time

from deeptutor.services.ai1_contract import (
    ContractError,
    check_policy,
    parse_request,
    validate_result,
)


class OperationService:
    def __init__(self, *, store, provider, profile, mode="off", continuous=False, clock=None):
        if (
            mode not in {"off", "enabled", "query-only", "store-isolated"}
            or type(continuous) is not bool
        ):
            raise ContractError("AI_PROFILE_UNAVAILABLE", 503)
        self.store = store
        self.provider = provider
        self._profile = copy.deepcopy(profile)
        self._mode = mode
        self._continuous = continuous
        self._clock = clock or (lambda: time.time_ns() // 1000000)

    def _admit(self, *, write=False):
        if self._mode == "store-isolated" or not self._continuous:
            raise ContractError("AI_STORE_ISOLATED", 503)
        if self._mode == "off" or (write and self._mode != "enabled"):
            raise ContractError("AI_DISABLED", 503)

    @staticmethod
    def _id(operation_id):
        if not isinstance(operation_id, str) or not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", operation_id
        ):
            raise ContractError()

    def _prepare(self, snapshot):
        failure = check_policy(snapshot, self._profile, self._clock())
        if failure:
            raise ContractError(failure, 503)
        # Only the restricted Provider may serialize and bound the complete request.
        # It must be pure: this phase never invokes a transport or resolves secrets.
        prepared = self.provider.prepare(copy.deepcopy(snapshot))
        failure = check_policy(snapshot, self._profile, self._clock())
        if failure:
            raise ContractError(failure, 503)
        return prepared

    def receive(self, operation_id, raw):
        self._admit(write=True)
        self._id(operation_id)
        snapshot = parse_request(raw)
        existing = self.store.get(operation_id)
        if existing is not None:
            # Historical terminal replay is independent of today's price validity.
            return self.store.accept(operation_id, snapshot)
        failure = None
        try:
            self._prepare(snapshot)
        except ContractError as error:
            failure = (
                error.code
                if error.code in {"PREVIEW_STALE", "VALIDATION_ERROR", "PROFILE_UNAVAILABLE"}
                else "PROFILE_UNAVAILABLE"
            )
        operation, created = self.store.accept(operation_id, snapshot)
        if failure:
            self.store.reject(operation_id, failure)
            operation = self.store.get(operation_id)
        return operation, created

    def get(self, operation_id):
        self._admit()
        self._id(operation_id)
        operation = self.store.get(operation_id)
        if operation is None:
            raise ContractError("RESOURCE_NOT_FOUND", 404)
        return operation

    async def dispatch(self, operation_id):
        self._admit(write=True)
        self._id(operation_id)
        prepared = None

        def policy(snapshot):
            nonlocal prepared
            try:
                prepared = self._prepare(snapshot)
            except ContractError as error:
                return (
                    error.code
                    if error.code in {"PREVIEW_STALE", "VALIDATION_ERROR", "PROFILE_UNAVAILABLE"}
                    else "PROFILE_UNAVAILABLE"
                )
            return None

        # A lost commit acknowledgement raises before transport; replay cannot re-claim.
        owner = self.store.claim(operation_id, policy)
        if owner is None:
            return
        snapshot = self.store.snapshot(operation_id)
        if check_policy(snapshot, self._profile, self._clock()) is not None:
            self.store.finish(operation_id, owner, "unknown")
            return
        try:
            value, usage = await self.provider.send(prepared)
            result, usage = validate_result(value, usage)
        except asyncio.CancelledError:
            try:
                self.store.finish(operation_id, owner, "unknown")
            except Exception:
                # A storage failure may leave sending, but must never reset the claim.
                pass
            raise
        except Exception:
            # No retry, raw error text or not-sent inference after the durable claim.
            self.store.finish(operation_id, owner, "unknown")
            return
        self.store.finish(operation_id, owner, "succeeded", result=result, usage=usage)
