"""ZFS zpool status."""

import json
import re
import shlex
from subprocess import DEVNULL, PIPE, Popen

from .exceptions import Linux2MqttException


class ZPoolException(Linux2MqttException):
    """Generic ZPool exception occurred."""


def parse_zfs_size(size_str: str) -> float:
    """Convert a ZFS human-readable size string to GB.

    Parameters
    ----------
    size_str
        Human-readable size like "267M", "696G", "4.02T"

    Returns
    -------
    float
        Size in GB

    """
    units = {"K": 1e-6, "M": 1e-3, "G": 1.0, "T": 1e3, "P": 1e6}
    match = re.match(r"^([\d.]+)\s*([KMGTP])", size_str)
    if match:
        return float(match.group(1)) * units[match.group(2)]
    return 0.0


class ZPool:
    """Represents a single ZFS pool."""

    pool_name: str
    attributes: dict

    def __init__(self, pool_name: str):
        """Initialize with pool name."""
        self.pool_name = pool_name
        self.attributes = {}

    @staticmethod
    def get_all_pools() -> list[str]:
        """Get all available ZFS pool names.

        Returns
        -------
        list[str]
            Pool names, or empty list if zpool is not available

        """
        try:
            command = shlex.split("zpool status -j")
            with Popen(command, stdout=PIPE, stderr=DEVNULL, text=True) as proc:
                stdout, _ = proc.communicate(timeout=30)
                if proc.returncode != 0:
                    return []
                data = json.loads(stdout)
                return list(data.get("pools", {}).keys())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []

    def _collect_leaf_stats(self, vdev: dict) -> tuple[int, int, int, int]:
        """Recursively walk vdev tree, summing errors from leaf disks.

        Returns
        -------
        tuple[int, int, int, int]
            (read_errors, write_errors, checksum_errors, degraded_devices)

        """
        read_errors = 0
        write_errors = 0
        checksum_errors = 0
        degraded = 0

        children = vdev.get("vdevs", {})
        if not children:
            # Leaf disk
            read_errors += int(vdev.get("read_errors", 0))
            write_errors += int(vdev.get("write_errors", 0))
            checksum_errors += int(vdev.get("checksum_errors", 0))
            if vdev.get("state", "ONLINE") != "ONLINE":
                degraded += 1
        else:
            for child in children.values():
                r, w, c, d = self._collect_leaf_stats(child)
                read_errors += r
                write_errors += w
                checksum_errors += c
                degraded += d

        return read_errors, write_errors, checksum_errors, degraded

    def parse_attributes(self) -> None:
        """Parse zpool status JSON for this pool."""
        command = shlex.split("zpool status -j")
        with Popen(command, stdout=PIPE, stderr=DEVNULL, text=True) as proc:
            stdout, stderr = proc.communicate(timeout=30)
            if proc.returncode != 0:
                raise ZPoolException(
                    f"zpool status failed: {proc.returncode}: '{stderr}'"
                )
            data = json.loads(stdout)

        pool_data = data.get("pools", {}).get(self.pool_name)
        if pool_data is None:
            raise ZPoolException(f"Pool '{self.pool_name}' not found")

        self.attributes = {}

        # Pool state
        state = pool_data.get("state", "UNKNOWN")
        self.attributes["pool_state"] = state

        # Error count
        self.attributes["error_count"] = int(pool_data.get("error_count", 0))

        # Scrub stats
        scan_stats = pool_data.get("scan_stats", {})
        if scan_stats:
            self.attributes["scrub_state"] = scan_stats.get("state", "NONE")
            self.attributes["scrub_errors"] = int(scan_stats.get("errors", 0))
            self.attributes["last_scrub"] = scan_stats.get("end_time", "Never")
        else:
            self.attributes["scrub_state"] = "NONE"
            self.attributes["scrub_errors"] = 0
            self.attributes["last_scrub"] = "Never"

        # Root vdev capacity
        root_vdev = pool_data.get("vdevs", {}).get(self.pool_name, {})
        alloc_str = root_vdev.get("alloc_space", "0")
        total_str = root_vdev.get("total_space", "0")
        alloc_gb = parse_zfs_size(alloc_str)
        total_gb = parse_zfs_size(total_str)
        free_gb = total_gb - alloc_gb

        self.attributes["alloc_gb"] = round(alloc_gb, 1)
        self.attributes["total_gb"] = round(total_gb, 1)
        self.attributes["free_gb"] = round(free_gb, 1)
        self.attributes["percent"] = round(alloc_gb / total_gb * 100, 1) if total_gb > 0 else 0

        # Walk vdev tree for leaf disk errors
        read_err, write_err, cksum_err, degraded = self._collect_leaf_stats(root_vdev)
        self.attributes["total_read_errors"] = read_err
        self.attributes["total_write_errors"] = write_err
        self.attributes["total_checksum_errors"] = cksum_err
        self.attributes["degraded_devices"] = degraded

        # Score and status
        self._calculate_score(state)
        self._calculate_status()
        self.attributes["score"] = self.score
        self.attributes["status"] = self.status

    def _calculate_score(self, state: str) -> None:
        """Calculate health score for the pool."""
        score = 0

        # Pool state
        if state == "DEGRADED":
            score += 30
        elif state in ("FAULTED", "UNAVAIL"):
            score += 100

        # Errors
        score += self.attributes.get("total_read_errors", 0) * 2
        score += self.attributes.get("total_write_errors", 0) * 3
        score += self.attributes.get("total_checksum_errors", 0) * 3
        score += self.attributes.get("error_count", 0) * 5

        # Degraded devices
        score += self.attributes.get("degraded_devices", 0) * 20

        # Capacity
        percent = self.attributes.get("percent", 0)
        if percent > 95:
            score += 50
        elif percent > 90:
            score += 20

        # Scrub errors
        if self.attributes.get("scrub_errors", 0) > 0:
            score += 10

        self.score = score

    def _calculate_status(self) -> None:
        """Convert score to status string."""
        if self.score <= 10:
            self.status = "HEALTHY"
        elif self.score <= 20:
            self.status = "GOOD"
        elif self.score <= 50:
            self.status = "WARNING"
        else:
            self.status = "FAILING"
