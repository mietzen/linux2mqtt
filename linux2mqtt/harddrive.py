"""Hard drives."""

import json
import re
import shlex
from subprocess import DEVNULL, PIPE, Popen

from .exceptions import HardDriveException, Linux2MqttException


class HardDrive:
    """Base class for all harddrives to implement."""

    # parameters
    _attributes: dict | None
    device_id: str
    attributes: dict
    score: int
    status: str

    def __init__(self, device_id: str):
        """Initialize the hard drive metric.

        Parameters
        ----------
        device_id
            The device id from /dev/disk/by-id/

        """
        self.device_id = device_id
        self._attributes = None

    def _get_attributes(self) -> None:
        command = shlex.split(
            f"/usr/sbin/smartctl --info --all --json --nocheck standby /dev/disk/by-id/{self.device_id}"
        )
        with Popen(
            command,
            stdout=PIPE,
            stderr=DEVNULL,
            text=True,
        ) as proc:
            stdout, stderr = proc.communicate(timeout=30)

            if (proc.returncode & 3) != 0:
                raise HardDriveException(
                    f"Something went wrong with smartctl: {proc.returncode}: '{stderr}'"
                )

            raw_json_data = json.loads(stdout)

        self._attributes = raw_json_data

    def parse_attributes(self) -> None:
        """Hard Drive specific parse function depending on results from smartctl."""
        raise Linux2MqttException from NotImplementedError

    def get_score(self) -> None:
        """Hard Drive specific score function depending on results from smartctl."""
        raise Linux2MqttException from NotImplementedError

    def get_status(self) -> None:
        """Convert the score to an Arbitrary Classification Set by developer."""
        if self.score <= 10:
            self.status = "HEALTHY"
        elif self.score <= 20:
            self.status = "GOOD"
        elif self.score <= 50:
            self.status = "WARNING"
        else:
            self.status = "FAILING"


class SataDrive(HardDrive):
    """For ATA Drives."""

    def parse_attributes(self) -> None:
        """Parse out attributes from smartctl where available."""
        self.attributes = {}
        self._get_attributes()
        ata_smart_attributes = [
            ("reallocated_sector_count", 5),
            ("command_timeout", 38),
            ("reported_uncorrectable_errors", 187),
            ("current_pending_sector", 197),
            ("offline_uncorrectable", 198),
            ("udma_crc_error_count", 199),
        ]

        self.attributes["model_name"] = self._attributes["model_name"]  # type: ignore[index]
        self.attributes["device"] = self._attributes["device"]["name"]  # type: ignore[index]
        self.attributes["size_tb"] = (
            self._attributes["user_capacity"]["bytes"] / 1000000000000  # type: ignore[index]
        )  # type: ignore[index]
        if "temperature" in self._attributes:  # type: ignore[operator]
            self.attributes["temperature"] = self._attributes["temperature"]["current"]  # type: ignore[index]
        self.attributes["smart_status"] = (
            "Healthy" if self._attributes["smart_status"]["passed"] else "Failed"  # type: ignore[index]
        )  # type: ignore[index]
        self.attributes["power_on_time"] = self._attributes["power_on_time"]["hours"]  # type: ignore[index]
        self.attributes["power_cycle_count"] = self._attributes["power_cycle_count"]  # type: ignore[index]

        new_data = {
            item["id"]: item
            for item in self._attributes["ata_smart_attributes"]["table"]  # type: ignore[index]
        }  # type: ignore[index]
        for name, key in ata_smart_attributes:
            tmp = new_data[key]["raw"]["value"] if new_data.get(key) else None
            if tmp is not None:
                self.attributes[name] = tmp

        self.get_score()
        self.get_status()
        self.attributes["score"] = self.score
        self.attributes["status"] = self.status

    def get_score(self) -> None:
        """ATA Drive specific score function depending on results from smartctl."""
        score = 0
        score += self.attributes.get("reallocated_sector_count", 0) * 2
        score += self.attributes.get("current_pending_sector", 0) * 3
        if self.attributes.get("current_pending_sector", 0) > 10:
            score += 30

        score += self.attributes.get("offline_uncorrectable", 0) * 3
        score += self.attributes.get("reported_uncorrectable_errors", 0) * 2
        score += self.attributes.get("command_timeout", 0) * 1.5
        score += min(self.attributes.get("udma_crc_error_count", 0), 10)

        self.score = score


class NVME(HardDrive):
    """For NVME Drives."""

    def parse_attributes(self) -> None:
        """Parse NVME Smartctl attributes."""
        self.attributes = {}
        self._get_attributes()
        nvme_smart_attributes = [
            "critical_warning",
            "percentage_used",
            "power_on_hours",
            "power_cycles",
            "media_errors",
            "num_err_log_entries",
            "critical_comp_time",
            "warning_temp_time",
            "available_spare",
            "available_spare_threshold",
        ]

        self.attributes["model_name"] = self._attributes["model_name"]  # type: ignore[index]
        self.attributes["device"] = self._attributes["device"]["name"]  # type: ignore[index]
        self.attributes["size_tb"] = (
            self._attributes["user_capacity"]["bytes"] / 1000000000000  # type: ignore[index]
        )  # type: ignore[index]
        if "temperature" in self._attributes:  # type: ignore[operator]
            self.attributes["temperature"] = self._attributes["temperature"]["current"]  # type: ignore[index]
        self.attributes["smart_status"] = (
            "Healthy" if self._attributes["smart_status"]["passed"] else "Failed"  # type: ignore[index]
        )  # type: ignore[index]

        for key in nvme_smart_attributes:
            tmp = self._attributes["nvme_smart_health_information_log"].get(key)  # type: ignore[index]
            if tmp is not None:
                self.attributes[key] = tmp

        self.get_score()
        self.get_status()
        self.attributes["score"] = self.score
        self.attributes["status"] = self.status

    def get_score(self) -> None:
        """Score specific for NVME Drives."""
        score = 0

        # Critical warnings (bitmask)
        if self.attributes.get("critical_warning") != 0:
            score += 100  # Any critical flag = high risk

        # NAND wear
        if self.attributes.get("percentage_used", 0) > 90:
            score += 50
        elif self.attributes.get("percentage_used", 0) > 80:
            score += 20
        elif self.attributes.get("percentage_used", 0) > 70:
            score += 10

        # Media/data errors
        score += self.attributes.get("media_errors", 0) * 5

        # Error log entries
        score += min(self.attributes.get("num_err_log_entries", 0), 50)  # cap at 50

        # Temperature issues
        if self.attributes.get("critical_comp_time", 0) > 0:
            score += 30
        elif self.attributes.get("warning_temp_time", 0) > 0:
            score += 10

        # Available spare
        if self.attributes.get("available_spare", 0) < self.attributes.get(
            "available_spare_threshold", 0
        ):
            score += 30

        self.score = score


def get_hard_drive(device_name: str) -> HardDrive:
    """Determine the hard drive type.

    Returns
    -------
    HardDrive
        The specific hard drive type for drive id

    """

    ata_regex = r"^ata.*(?<!part\d)$"
    nvme_regex = r"^nvme-eui.*(?<!part\d)$"

    r1 = re.compile(ata_regex)
    r2 = re.compile(nvme_regex)

    if r1.match(device_name):
        return SataDrive(device_name)
    elif r2.match(device_name):
        return NVME(device_name)
    else:
        raise HardDriveException("Harddrive ID not supported")
