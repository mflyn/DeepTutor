"""AI3 uses the AI1 journal/claim machinery with no default real Provider.

Only explicit in-process Provider injection is supported for local synthetic
qualification. No settings, credentials, SDK fallback or AI1 counter is loaded.
A separately qualified AI3 production configuration is not implemented here.
"""

import rfc8785

from deeptutor.services.ai1_contract import ContractError
from deeptutor.services.ai1_operation import OperationService as Ai1OperationService
from deeptutor.services.ai3_contract import SCHEMA_VERSION, parse_request


class OperationService(Ai1OperationService):
    _schema_version = SCHEMA_VERSION
    _parse_request = staticmethod(parse_request)

    def _prepare(self, snapshot):
        # Revalidate the closed profile and digest under the durable claim lock.
        parse_request(rfc8785.dumps(snapshot))
        if self.provider is None:
            raise ContractError("PROFILE_UNAVAILABLE", 503)
        return super()._prepare(snapshot)
