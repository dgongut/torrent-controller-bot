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

	if client_name == "qbittorrent":
		from torrent_clients.qbittorrent_client import QBittorrentClient
		return QBittorrentClient(
			host=config.TORRENT_CLIENT_HOST,
			port=int(config.TORRENT_CLIENT_PORT or 8080),
			username=config.TORRENT_CLIENT_USER,
			password=config.TORRENT_CLIENT_PASSWORD,
			protocol=config.TORRENT_CLIENT_PROTOCOL,
		)

	if client_name == "deluge":
		from torrent_clients.deluge_client import DelugeClient
		return DelugeClient(
			host=config.TORRENT_CLIENT_HOST,
			port=int(config.TORRENT_CLIENT_PORT or 8112),
			password=config.TORRENT_CLIENT_PASSWORD,
			protocol=config.TORRENT_CLIENT_PROTOCOL,
		)

	if client_name == "download_station":
		from torrent_clients.synology_client import SynologyClient
		default_port = 5001 if config.TORRENT_CLIENT_PROTOCOL == "https" else 5000
		return SynologyClient(
			host=config.TORRENT_CLIENT_HOST,
			port=int(config.TORRENT_CLIENT_PORT or default_port),
			username=config.TORRENT_CLIENT_USER,
			password=config.TORRENT_CLIENT_PASSWORD,
			protocol=config.TORRENT_CLIENT_PROTOCOL,
			api_path=config.TORRENT_CLIENT_RPC_PATH or "/webapi",
		)

	raise TorrentClientError(f"Unsupported torrent client: {client_name}")
