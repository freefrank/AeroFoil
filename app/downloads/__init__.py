from .prowlarr import ProwlarrClient, pick_best_result, filter_results
from .torrent_client import test_torrent_client, add_torrent, list_completed, remove_torrent
from .client import test_download_client, get_supported_download_clients, get_download_client_capabilities, get_download_client_diagnostics
from .manager import run_downloads_job, manual_search_update, queue_download_url, search_update_options, check_completed_downloads, get_downloads_state, get_active_downloads, get_download_ui_visibility, get_configured_download_protocols, filter_download_search_results, sort_download_search_results, remove_pending_download, remove_duplicate_download, dismiss_duplicate_download

__all__ = [
    "ProwlarrClient",
    "pick_best_result",
    "filter_results",
    "test_download_client",
    "get_supported_download_clients",
    "get_download_client_capabilities",
    "get_download_client_diagnostics",
    "test_torrent_client",
    "add_torrent",
    "remove_torrent",
    "list_completed",
    "run_downloads_job",
    "manual_search_update",
    "queue_download_url",
    "check_completed_downloads",
    "get_downloads_state",
    "get_active_downloads",
    "search_update_options",
    "get_download_ui_visibility",
    "get_configured_download_protocols",
    "filter_download_search_results",
    "sort_download_search_results",
    "remove_pending_download",
    "remove_duplicate_download",
    "dismiss_duplicate_download",
]
