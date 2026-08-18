"""Factory that instantiates the configured torrent client implementation"""

from torrent_clients.base import TorrentClientError


def create_client(config):
	"""Creates a TorrentClient based on config.TORRENT_CLIENT.
	New torrent managers only need to be registered here."""
	client_name = (config.TORRENT_CLIENT or "transmission").lower()

	if client_name == "transmission":
		from torrent_clients.transmission_client import TransmissionClient
		return TransmissionClient(
			host=config.TORRENT_CLIENT_HOST,
			port=int(config.TORRENT_CLIENT_PORT or 9091),
			username=config.TORRENT_CLIENT_USER,
			password=config.TORRENT_CLIENT_PASSWORD,
			protocol=config.TORRENT_CLIENT_PROTOCOL,
			rpc_path=config.TORRENT_CLIENT_RPC_PATH or "/transmission/rpc",
		)

	raise TorrentClientError(f"Unsupported torrent client: {client_name}")
