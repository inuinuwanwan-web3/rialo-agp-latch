"""Fixed read methods and a second allowlist at the actual transport boundary."""
from .config import resolve_launch
from .gateway import McpGateway
from .watch_audit import CURRENT


class ObservationTransport(McpGateway):
    read_attempts = 1  # The watcher owns exponential backoff, including startup.
    list_interval = 0  # Poll completion + configured interval controls cadence.

    def __init__(self, settings):
        super().__init__(settings, allow_writes=False)

    @staticmethod
    def _check(method, params):
        if method == 'initialize':
            return
        if method == 'notifications/initialized' and params == {}:
            return
        if method == 'tools/call':
            name, args = params.get('name'), params.get('arguments', {})
            if name in {'list_tracks', 'my_race', 'sigil_balance'} and args == {}:
                return
            if name == 'track_state' and (args == {} or (
                    isinstance(args, dict) and set(args) == {'trackId'}
                    and isinstance(args['trackId'], str) and args['trackId'])):
                return
        raise RuntimeError('Read-only observation operation blocked')

    def request(self, method, params):
        audit = CURRENT.get()
        try:
            self._check(method, params)
        except RuntimeError:
            if audit is not None:
                audit.violation(method, params)
            raise
        try:
            return super().request(method, params)
        except Exception as error:
            if audit is not None:
                audit.transport_error(error)
            raise

    def _write(self, message, deadline):
        audit = CURRENT.get()
        method, params = message.get('method'), message.get('params', {})
        try:
            self._check(method, params)
        except RuntimeError:
            if audit is not None:
                audit.violation(method, params)
            raise
        key = audit.before_send(method, params) if audit is not None else None
        result = super()._write(message, deadline)
        if audit is not None:
            audit.after_send(key)
        return result


class ObservationReader:
    """No generic tool call, write permission option, or write methods."""
    def __init__(self, settings):
        self.__transport = ObservationTransport(settings)

    def list_tracks(self):
        return self.__transport.call('list_tracks')

    def my_race(self):
        return self.__transport.call('my_race')

    def sigil_balance(self):
        return self.__transport.call('sigil_balance')

    def track_state(self, track_id):
        return self.__transport.call('track_state', {'trackId': track_id})

    def close(self):
        self.__transport.close()


def credential_values(settings):
    """Known auth values stay in memory only, for response redaction."""
    import re
    _, _, env = resolve_launch(settings)
    values = []
    for key, value in env.items():
        if re.search(r'TOKEN|SECRET|PASSWORD|AUTH|PRIVATE|API_KEY', key, re.I) and value:
            values.append(value)
            if value.strip().lower().startswith(('bearer ', 'basic ')):
                values.append(value.strip().split(None, 1)[1])
    return values
