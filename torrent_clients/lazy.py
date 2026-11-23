"""A torrent client that connects when the manager first answers.

Every client connects (or logs in) when it is created, so a bot started while
its manager was down had no client at all and exited, and with restart: always
the container went up and down for as long as the manager was away.

LazyClient stands in for the real client from the start. Until the manager
answers, each call first tries to connect and, when that fails, raises the
same TorrentClientError a call to an unreachable manager raises once
connected: the bot already handles that (screens tell the error, adds are
queued, the monitor tries again on its next pass). Before the first
connection the capabilities (supports_*...) are read from the class. A client
that settles one only once connected (Synology sets file_priority_levels by
DSM version) is read correctly by the bot all the same: the screens that use
it ask the manager for a torrent first, which connects

Once connected it only passes everything through to the real client: nothing
changes in how a manager that goes away later is handled.
"""

import threading
import time

from torrent_clients.base import LoginRefusedError, TorrentClientError

# A connection that just failed is not tried again for this long: every tap on
# a button would otherwise wait for its own timeout (up to 30 s when the host
# does not answer at all) while the manager is down
RETRY_SECONDS = 10

# A manager that refused the login is asked again much less often, and less
# each time: it will not change its mind by being asked, and it counts the
# failures (qBittorrent bans the address after 5, DSM blocks it after 10 in 5
# minutes, Transmission stops answering it after 100 until it is restarted).
# Once banned, even the right password is refused. The last wait is repeated
REFUSED_RETRY_SECONDS = (300, 900, 3600)


class LazyClient:
	def __init__(self, factory, client_class, clock=time.monotonic):
		"""factory builds the real client (connecting to the manager) and
		client_class is its class, for the capabilities before it exists"""
		object.__setattr__(self, "_factory", factory)
		object.__setattr__(self, "_class", client_class)
		object.__setattr__(self, "_clock", clock)
		object.__setattr__(self, "_lock", threading.Lock())
		object.__setattr__(self, "_instance", None)
		object.__setattr__(self, "_version", None)
		object.__setattr__(self, "_failed_at", None)
		object.__setattr__(self, "_retry_in", 0)
		object.__setattr__(self, "_refusals", 0)
		object.__setattr__(self, "_error", None)

	@property
	def connected(self):
		return self._instance is not None

	@property
	def version(self):
		"""What the manager said it is when it first answered, or None"""
		return self._version

	@property
	def last_error(self):
		"""Why the last connection failed, while not connected"""
		return None if self.connected else self._error

	@property
	def refused(self):
		"""Whether the manager answered the last attempt and refused the bot
		(see LoginRefusedError): the configuration has to change"""
		return isinstance(self.last_error, LoginRefusedError)

	def connect(self):
		"""The real client, connecting first if needed. Raises
		TorrentClientError while the manager does not answer. One attempt at
		a time: a call that comes meanwhile does not queue behind it (up to
		30 s on a host that does not answer) and fails at once like the last
		attempt did"""
		instance = self._instance
		if instance is not None:
			return instance
		if not self._lock.acquire(blocking=False):
			if self._instance is not None:
				return self._instance
			raise TorrentClientError(str(self._error or "Connecting to the torrent client"))
		try:
			if self._instance is not None:
				return self._instance
			if self._failed_at is not None and self._clock() - self._failed_at < self._retry_in:
				raise TorrentClientError(str(self._error))
			try:
				instance = self._factory()
				# Some clients only log in on their first call: the version
				# is what proves the manager answers
				version = instance.test_connection()
			except TorrentClientError as e:
				self._fail(e)
				raise
			except Exception as e:
				error = TorrentClientError(f"Cannot connect to the torrent client: {e}")
				self._fail(error)
				raise error
			object.__setattr__(self, "_version", version)
			object.__setattr__(self, "_instance", instance)
			object.__setattr__(self, "_failed_at", None)
			object.__setattr__(self, "_refusals", 0)
			object.__setattr__(self, "_error", None)
			return instance
		finally:
			self._lock.release()

	def _fail(self, error):
		if isinstance(error, LoginRefusedError):
			retry_in = REFUSED_RETRY_SECONDS[min(self._refusals, len(REFUSED_RETRY_SECONDS) - 1)]
			object.__setattr__(self, "_refusals", self._refusals + 1)
		else:
			retry_in = RETRY_SECONDS
			object.__setattr__(self, "_refusals", 0)
		object.__setattr__(self, "_retry_in", retry_in)
		object.__setattr__(self, "_failed_at", self._clock())
		object.__setattr__(self, "_error", error)

	def __getattr__(self, name):
		# Only called for what LazyClient itself does not have
		instance = self._instance
		if instance is not None:
			return getattr(instance, name)
		attribute = getattr(self._class, name)
		if not callable(attribute):
			return attribute  # A capability
		def call(*args, **kwargs):
			return getattr(self.connect(), name)(*args, **kwargs)
		return call

	def __setattr__(self, name, value):
		instance = self._instance
		if instance is None:
			raise AttributeError(f"Cannot set {name}: the torrent client is not connected yet")
		setattr(instance, name, value)

	def __delattr__(self, name):
		instance = self._instance
		if instance is None:
			raise AttributeError(f"Cannot delete {name}: the torrent client is not connected yet")
		delattr(instance, name)
