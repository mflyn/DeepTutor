"""AI3 envelope on the bounded SDK path; no qualified counter installed."""

from deeptutor.services.ai1_provider import RestrictedCompletionProvider as Ai1Provider
from deeptutor.services.ai3_contract import parse_request


class RestrictedCompletionProvider(Ai1Provider):
    _parse_request = staticmethod(parse_request)
