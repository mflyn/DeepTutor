"""AI3 uses the AI1 journal/claim machinery with no default real Provider.

Formal lifecycle assembly lives in ai3_runtime. This service itself loads no
settings, credentials, SDK fallback or counter. Real counter qualification is
still a separate enabling prerequisite; synthetic injection does not prove it.
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
