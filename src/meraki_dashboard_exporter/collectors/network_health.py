"""Medium-tier network health metric collector."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from ..core.collector import MetricCollector
from ..core.constants import MetricName, UpdateTier
from ..core.logging import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)


class NetworkHealthCollector(MetricCollector):
    """Collector for medium-moving network health metrics."""

    # Network health data updates at medium frequency
    update_tier: UpdateTier = UpdateTier.MEDIUM

    def _set_metric_value(self, metric_name: str, labels: dict[str, str], value: float) -> None:
        """Safely set a metric value with validation.

        Parameters
        ----------
        metric_name : str
            Name of the metric attribute.
        labels : dict[str, str]
            Labels to apply to the metric.
        value : float
            Value to set.

        """
        metric = getattr(self, metric_name, None)
        if metric is None:
            logger.debug(
                "Metric not available",
                metric_name=metric_name,
            )
            return

        try:
            metric.labels(**labels).set(value)
        except Exception:
            logger.exception(
                "Failed to set metric value",
                metric_name=metric_name,
                labels=labels,
                value=value,
            )

    def _initialize_metrics(self) -> None:
        """Initialize network health metrics."""
        # RF channel utilization metrics per AP
        self._ap_utilization_2_4ghz = self._create_gauge(
            "meraki_ap_channel_utilization_2_4ghz_percent",
            "2.4GHz channel utilization percentage per AP",
            labelnames=["network_id", "network_name", "serial", "name", "model", "type"],
        )

        self._ap_utilization_5ghz = self._create_gauge(
            "meraki_ap_channel_utilization_5ghz_percent",
            "5GHz channel utilization percentage per AP",
            labelnames=["network_id", "network_name", "serial", "name", "model", "type"],
        )

        # Network-wide average utilization
        self._network_utilization_2_4ghz = self._create_gauge(
            "meraki_network_channel_utilization_2_4ghz_percent",
            "Network-wide average 2.4GHz channel utilization percentage",
            labelnames=["network_id", "network_name", "type"],
        )

        self._network_utilization_5ghz = self._create_gauge(
            "meraki_network_channel_utilization_5ghz_percent",
            "Network-wide average 5GHz channel utilization percentage",
            labelnames=["network_id", "network_name", "type"],
        )

        # Network-wide wireless connection statistics
        self._network_connection_stats = self._create_gauge(
            MetricName.NETWORK_WIRELESS_CONNECTION_STATS,
            "Network-wide wireless connection statistics over the last 30 minutes (assoc/auth/dhcp/dns/success)",
            labelnames=["network_id", "network_name", "stat_type"],
        )

    async def _collect_impl(self) -> None:
        """Collect network health metrics."""
        try:
            # Get organizations
            if self.settings.org_id:
                org_ids = [self.settings.org_id]
            else:
                orgs = await asyncio.to_thread(self.api.organizations.getOrganizations)
                org_ids = [org["id"] for org in orgs]

            # Collect network health for each organization
            for org_id in org_ids:
                await self._collect_org_network_health(org_id)

        except Exception:
            logger.exception("Failed to collect network health metrics")

    async def _collect_org_network_health(self, org_id: str) -> None:
        """Collect network health metrics for an organization.

        Parameters
        ----------
        org_id : str
            Organization ID.

        """
        try:
            # Get all networks
            networks = await asyncio.to_thread(
                self.api.organizations.getOrganizationNetworks,
                org_id,
                total_pages="all",
            )

            # Collect health metrics for each network in batches
            # to avoid overwhelming the API connection pool
            batch_size = 10
            for i in range(0, len(networks), batch_size):
                batch = networks[i : i + batch_size]
                tasks = []

                # Use list comprehension for better performance
                tasks = [
                    self._collect_network_rf_health(network)
                    for network in batch
                    if "wireless" in network.get("productTypes", [])
                ]

                # Also collect connection stats for wireless networks
                connection_tasks = [
                    self._collect_network_connection_stats(network)
                    for network in batch
                    if "wireless" in network.get("productTypes", [])
                ]

                if tasks or connection_tasks:
                    await asyncio.gather(*(tasks + connection_tasks), return_exceptions=True)

        except Exception:
            logger.exception(
                "Failed to collect network health for organization",
                org_id=org_id,
            )

    async def _collect_network_rf_health(self, network: dict[str, Any]) -> None:
        """Collect RF health metrics for a network.

        Parameters
        ----------
        network : dict[str, Any]
            Network data.

        """
        network_id = network["id"]
        network_name = network.get("name", network_id)

        try:
            # Get channel utilization
            logger.debug(
                "Fetching channel utilization",
                network_id=network_id,
                network_name=network_name,
            )

            # Get AP names for lookup
            devices = await asyncio.to_thread(
                self.api.networks.getNetworkDevices,
                network_id,
            )
            device_names = {
                d["serial"]: d.get("name", d["serial"])
                for d in devices
                if d.get("model", "").startswith("MR")
            }

            channel_util = await asyncio.wait_for(
                asyncio.to_thread(
                    self.api.networks.getNetworkNetworkHealthChannelUtilization,
                    network_id,
                    total_pages="all",
                ),
                timeout=30.0,
            )

            if channel_util:
                # Track network-wide averages
                network_2_4ghz_total = {"total": 0, "wifi": 0, "non_wifi": 0, "count": 0}
                network_5ghz_total = {"total": 0, "wifi": 0, "non_wifi": 0, "count": 0}

                for ap_data in channel_util:
                    serial = ap_data.get("serial", "")
                    model = ap_data.get("model", "")
                    name = device_names.get(serial, serial)

                    # Process 2.4GHz (wifi0)
                    if "wifi0" in ap_data and ap_data["wifi0"]:
                        latest_2_4 = ap_data["wifi0"][0]  # Get most recent data
                        total_util = latest_2_4.get("utilization", 0)
                        wifi_util = latest_2_4.get("wifi", 0)
                        non_wifi_util = latest_2_4.get("non_wifi", 0)

                        # Per-AP metrics
                        self._set_metric_value(
                            "_ap_utilization_2_4ghz",
                            {
                                "network_id": network_id,
                                "network_name": network_name,
                                "serial": serial,
                                "name": name,
                                "model": model,
                                "type": "total",
                            },
                            total_util,
                        )

                        self._set_metric_value(
                            "_ap_utilization_2_4ghz",
                            {
                                "network_id": network_id,
                                "network_name": network_name,
                                "serial": serial,
                                "name": name,
                                "model": model,
                                "type": "wifi",
                            },
                            wifi_util,
                        )

                        self._set_metric_value(
                            "_ap_utilization_2_4ghz",
                            {
                                "network_id": network_id,
                                "network_name": network_name,
                                "serial": serial,
                                "name": name,
                                "model": model,
                                "type": "non_wifi",
                            },
                            non_wifi_util,
                        )

                        # Add to network totals
                        network_2_4ghz_total["total"] += total_util
                        network_2_4ghz_total["wifi"] += wifi_util
                        network_2_4ghz_total["non_wifi"] += non_wifi_util
                        network_2_4ghz_total["count"] += 1

                    # Process 5GHz (wifi1)
                    if "wifi1" in ap_data and ap_data["wifi1"]:
                        latest_5 = ap_data["wifi1"][0]  # Get most recent data
                        total_util = latest_5.get("utilization", 0)
                        wifi_util = latest_5.get("wifi", 0)
                        non_wifi_util = latest_5.get("non_wifi", 0)

                        # Per-AP metrics
                        self._set_metric_value(
                            "_ap_utilization_5ghz",
                            {
                                "network_id": network_id,
                                "network_name": network_name,
                                "serial": serial,
                                "name": name,
                                "model": model,
                                "type": "total",
                            },
                            total_util,
                        )

                        self._set_metric_value(
                            "_ap_utilization_5ghz",
                            {
                                "network_id": network_id,
                                "network_name": network_name,
                                "serial": serial,
                                "name": name,
                                "model": model,
                                "type": "wifi",
                            },
                            wifi_util,
                        )

                        self._set_metric_value(
                            "_ap_utilization_5ghz",
                            {
                                "network_id": network_id,
                                "network_name": network_name,
                                "serial": serial,
                                "name": name,
                                "model": model,
                                "type": "non_wifi",
                            },
                            non_wifi_util,
                        )

                        # Add to network totals
                        network_5ghz_total["total"] += total_util
                        network_5ghz_total["wifi"] += wifi_util
                        network_5ghz_total["non_wifi"] += non_wifi_util
                        network_5ghz_total["count"] += 1

                # Set network-wide averages
                if network_2_4ghz_total["count"] > 0:
                    self._set_metric_value(
                        "_network_utilization_2_4ghz",
                        {
                            "network_id": network_id,
                            "network_name": network_name,
                            "type": "total",
                        },
                        network_2_4ghz_total["total"] / network_2_4ghz_total["count"],
                    )

                    self._set_metric_value(
                        "_network_utilization_2_4ghz",
                        {
                            "network_id": network_id,
                            "network_name": network_name,
                            "type": "wifi",
                        },
                        network_2_4ghz_total["wifi"] / network_2_4ghz_total["count"],
                    )

                    self._set_metric_value(
                        "_network_utilization_2_4ghz",
                        {
                            "network_id": network_id,
                            "network_name": network_name,
                            "type": "non_wifi",
                        },
                        network_2_4ghz_total["non_wifi"] / network_2_4ghz_total["count"],
                    )

                if network_5ghz_total["count"] > 0:
                    self._set_metric_value(
                        "_network_utilization_5ghz",
                        {
                            "network_id": network_id,
                            "network_name": network_name,
                            "type": "total",
                        },
                        network_5ghz_total["total"] / network_5ghz_total["count"],
                    )

                    self._set_metric_value(
                        "_network_utilization_5ghz",
                        {
                            "network_id": network_id,
                            "network_name": network_name,
                            "type": "wifi",
                        },
                        network_5ghz_total["wifi"] / network_5ghz_total["count"],
                    )

                    self._set_metric_value(
                        "_network_utilization_5ghz",
                        {
                            "network_id": network_id,
                            "network_name": network_name,
                            "type": "non_wifi",
                        },
                        network_5ghz_total["non_wifi"] / network_5ghz_total["count"],
                    )

                logger.debug(
                    "Collected channel utilization",
                    network_id=network_id,
                    ap_count=len(channel_util),
                )
            else:
                logger.debug(
                    "No channel utilization data available",
                    network_id=network_id,
                )

        except TimeoutError:
            logger.error(
                "Timeout collecting RF health metrics",
                network_id=network_id,
                network_name=network_name,
            )
        except Exception as e:
            # Log at debug level if it's just not available (400/404 errors)
            error_str = str(e)
            if "400" in error_str or "404" in error_str or "Bad Request" in error_str:
                logger.debug(
                    "RF health metrics not available for network",
                    network_id=network_id,
                    network_name=network_name,
                    error=error_str,
                )
            else:
                logger.exception(
                    "Failed to collect RF health metrics",
                    network_id=network_id,
                    network_name=network_name,
                )

    async def _collect_network_connection_stats(self, network: dict[str, Any]) -> None:
        """Collect network-wide wireless connection statistics.

        Parameters
        ----------
        network : dict[str, Any]
            Network data.

        """
        network_id = network["id"]
        network_name = network.get("name", network_id)

        try:
            logger.debug(
                "Fetching network connection stats",
                network_id=network_id,
                network_name=network_name,
            )

            # Track API call
            self._track_api_call("getNetworkWirelessConnectionStats")

            # Use 30 minute (1800 second) timespan as minimum
            connection_stats = await asyncio.wait_for(
                asyncio.to_thread(
                    self.api.wireless.getNetworkWirelessConnectionStats,
                    network_id,
                    timespan=1800,  # 30 minutes
                ),
                timeout=10.0,  # 10 second timeout
            )

            # Handle empty response (no data in timespan)
            if not connection_stats:
                logger.debug(
                    "No connection stats data available",
                    network_id=network_id,
                    timespan="30m",
                )
                # Set all stats to 0 when no data
                for stat_type in ("assoc", "auth", "dhcp", "dns", "success"):
                    self._set_metric_value(
                        "_network_connection_stats",
                        {
                            "network_id": network_id,
                            "network_name": network_name,
                            "stat_type": stat_type,
                        },
                        0,
                    )
                return

            # Set metrics for each connection stat type
            for stat_type in ("assoc", "auth", "dhcp", "dns", "success"):
                value = connection_stats.get(stat_type, 0)
                self._set_metric_value(
                    "_network_connection_stats",
                    {
                        "network_id": network_id,
                        "network_name": network_name,
                        "stat_type": stat_type,
                    },
                    value,
                )

            logger.debug(
                "Successfully collected network connection stats",
                network_id=network_id,
                stats=connection_stats,
            )

        except TimeoutError:
            logger.error(
                "Timeout fetching network connection stats",
                network_id=network_id,
                network_name=network_name,
            )
        except Exception as e:
            # Log at debug level if it's just not available (400/404 errors)
            error_str = str(e)
            if "400" in error_str or "404" in error_str or "Bad Request" in error_str:
                logger.debug(
                    "Network connection stats not available",
                    network_id=network_id,
                    network_name=network_name,
                    error=error_str,
                )
            else:
                logger.exception(
                    "Failed to collect network connection stats",
                    network_id=network_id,
                    network_name=network_name,
                )
